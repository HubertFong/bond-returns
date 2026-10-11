"""Tests for fetch_fedinvest.py, using HTML in the layout FedInvest serves (Oct 2026).

Run from the repo root:  python -m unittest discover -s python/tests
"""
import pathlib
import sys
import unittest
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fetch_fedinvest import business_days_back, form_payload, parse, parse_form  # noqa: E402

# The Historical Prices page: a site-search form, then the price-date form with one
# ISO date field, a submit button and a hidden CSRF token.
FORM_PAGE = """<html><body>
<form id="gs" method="get" action="https://search.treas.gov/search">
  <input type="text" name="q" /><input type="hidden" name="sort" value="date:D:L:d1" /></form>
<form id="fromToDate" action="/GA-FI/FedInvest/selectSecurityPriceDate" method="post">
  <td><input id="priceDate" name="priceDate" type="date" value="2026-10-09" required="required"/></td>
  <input type="submit" value="Show Prices" class="action" name="submit" />
  <div><input type="hidden" name="_csrf" value="tok-abc" /></div>
</form></body></html>"""


def result_page(rows: str, header: str = "<th>CUSIP</th><th>SECURITY TYPE</th><th>RATE</th>"
                "<th>MATURITY DATE</th><th>CALL DATE</th><th>BUY</th><th>SELL</th><th>END OF DAY</th>") -> str:
    return f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"


ROWS = """
<tr><td>912797LB1</td><td>MARKET BASED BILL</td><td>0.000</td><td>11/13/2026</td><td></td>
    <td>99.512000</td><td>99.508000</td><td>0.000000</td></tr>
<tr><td>91282CLW9</td><td>MARKET BASED NOTE</td><td>4.250</td><td>11/15/2034</td><td></td>
    <td>99.218750</td><td>99.187500</td><td>99.203125</td></tr>
<tr><td>912810TX6</td><td>MARKET BASED BOND</td><td>4.750</td><td>11/15/2053</td><td></td>
    <td>97.531250</td><td>97.468750</td><td>97.500000</td></tr>
<tr><td>91282CGW5</td><td>TIPS</td><td>1.250</td><td>04/15/2028</td><td></td>
    <td>98.765625</td><td>98.734375</td><td>98.750000</td></tr>
<tr><td>91282CAB7</td><td>MARKET BASED NOTE</td><td>0.125</td><td>03/31/2027</td><td></td>
    <td>98.500000</td><td>98.468750</td><td>0.000000</td></tr>"""


class FormTests(unittest.TestCase):
    def test_finds_the_price_date_form_not_the_search_form(self):
        form = parse_form(FORM_PAGE)
        self.assertEqual(form["action"], "/GA-FI/FedInvest/selectSecurityPriceDate")

    def test_fills_the_form_like_a_browser(self):
        self.assertEqual(form_payload(parse_form(FORM_PAGE), date(2026, 10, 9)),
                         [("priceDate", "2026-10-09"), ("submit", "Show Prices"), ("_csrf", "tok-abc")])

    def test_page_without_the_form(self):
        self.assertIsNone(parse_form("<html><body>Down for maintenance</body></html>"))


class TableTests(unittest.TestCase):
    def test_keeps_fixed_coupon_notes_and_bonds_only(self):
        self.assertEqual([r["cusip"] for r in parse(result_page(ROWS))], ["91282CLW9", "912810TX6", "91282CAB7"])

    def test_record_fields(self):
        r = parse(result_page(ROWS))[0]
        self.assertEqual(r, {"cusip": "91282CLW9", "type": "MARKET BASED NOTE", "rate": 0.0425,
                             "maturity": "2034-11-15", "buy": 99.21875, "sell": 99.1875, "eod": 99.203125})

    def test_low_coupon_is_a_percent_and_missing_eod_uses_the_mid(self):
        r = parse(result_page(ROWS))[2]
        self.assertEqual(r["rate"], 0.00125)                     # 0.125%, not 12.5%
        self.assertEqual(r["eod"], round((98.5 + 98.46875) / 2, 6))

    def test_header_row_sets_the_column_order(self):
        header = ("<th>SECURITY TYPE</th><th>CUSIP</th><th>RATE</th><th>MATURITY DATE</th>"
                  "<th>CALL DATE</th><th>BUY</th><th>SELL</th><th>END OF DAY</th>")
        row = ("<tr><td>MARKET BASED NOTE</td><td>91282CLW9</td><td>4.250</td><td>11/15/2034</td>"
               "<td></td><td>99.2</td><td>99.1</td><td>99.15</td></tr>")
        self.assertEqual(parse(result_page(row, header))[0]["cusip"], "91282CLW9")

    def test_no_table_means_no_prices(self):
        self.assertEqual(parse("<html><body>No prices for this date</body></html>"), [])

    def test_a_format_change_is_refused(self):
        row = ("<tr><td>91282CLW9</td><td>MARKET BASED NOTE</td><td>42.50</td><td>11/15/2034</td>"
               "<td></td><td>99</td><td>98.9</td><td>98.95</td></tr>")
        with self.assertRaises(ValueError):
            parse(result_page(row))


class DateTests(unittest.TestCase):
    def test_weekends_are_skipped_and_holidays_fall_back(self):
        # Sunday 11 Oct 2026 -> Friday first. Monday 12 Oct (Columbus Day) -> Monday, then Friday.
        self.assertEqual(list(business_days_back(date(2026, 10, 11), 2)), [date(2026, 10, 9), date(2026, 10, 8)])
        self.assertEqual(list(business_days_back(date(2026, 10, 12), 2)), [date(2026, 10, 12), date(2026, 10, 9)])


if __name__ == "__main__":
    unittest.main()
