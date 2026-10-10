"""Parser tests for fetch_fedinvest.py against a fixture in FedInvest's CSV layout.
Run: python -m unittest discover -s python/tests"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from datetime import date  # noqa: E402

from fetch_fedinvest import form_payload, parse, parse_form  # noqa: E402

FIXTURE = pathlib.Path(__file__).with_name("fedinvest_sample.csv").read_text()


class ParseTests(unittest.TestCase):
    def test_keeps_only_fixed_coupon_notes_and_bonds(self):
        rows = parse(FIXTURE)
        self.assertEqual([r["cusip"] for r in rows], ["91282CLW9", "912810TX6", "91282CMA6"])
        self.assertTrue(all(r["type"] in {"MARKET BASED NOTE", "MARKET BASED BOND"} for r in rows))

    def test_fields(self):
        r = parse(FIXTURE)[0]
        self.assertEqual(r["rate"], 0.0425)
        self.assertEqual(r["maturity"], "2034-11-15")
        self.assertEqual((r["buy"], r["sell"], r["eod"]), (99.21875, 99.1875, 99.203125))

    def test_mid_when_end_of_day_missing(self):
        r = parse(FIXTURE)[2]
        self.assertEqual(r["eod"], round((100.5 + 100.4375) / 2, 6))

    def test_percent_rate_and_html_are_handled(self):
        self.assertEqual(parse("91282CLW9,MARKET BASED NOTE,4.250%,11/15/2034,,99,98.9,98.95")[0]["rate"], 0.0425)
        self.assertEqual(parse("<html><body>No data for this date</body></html>"), [])

    def test_format_change_is_detected(self):
        with self.assertRaises(ValueError):
            parse("91282CLW9,MARKET BASED NOTE,42.50000,11/15/2034,,99,98.9,98.95")


FORM_HTML = """<html><body>
<form action="/GA-FI/FedInvest/searchSecurityPrice" method="post" class="search">
  <input type="text" name="q"></form>
<form action="/GA-FI/FedInvest/securityPriceDetail" method="post">
  <input type="hidden" name="_csrf" value="tok-123">
  <select name="priceDateMonth"><option value="1">January</option><option value="10">October</option></select>
  <select name="priceDateDay"><option value="01">1</option><option value="09">9</option></select>
  <select name="priceDateYear"><option value="2025">2025</option><option value="2026">2026</option></select>
  <input type="radio" name="fileType" value="html" checked> <input type="radio" name="fileType" value="csv">
  <input type="submit" name="btnSubmit" value="Show Prices">
</form></body></html>"""


class FormTests(unittest.TestCase):
    def test_finds_the_price_form_and_fills_it_like_a_browser(self):
        form = parse_form(FORM_HTML)
        self.assertEqual(form["action"], "/GA-FI/FedInvest/securityPriceDetail")
        self.assertEqual(form_payload(form, date(2026, 10, 9)), [
            ("_csrf", "tok-123"), ("priceDateMonth", "10"), ("priceDateDay", "09"),
            ("priceDateYear", "2026"), ("fileType", "csv"), ("btnSubmit", "Show Prices")])

    def test_fields_outside_a_form_tag(self):
        form = parse_form('<div><select name="priceDateDay"><option value="9">9</option></select>'
                          '<input type="hidden" name="_csrf" value="t"></div>')
        self.assertEqual(form_payload(form, date(2026, 10, 9))[:2], [("priceDateDay", "9"), ("_csrf", "t")])

    def test_no_form(self):
        self.assertIsNone(parse_form("<html><body>maintenance</body></html>"))


if __name__ == "__main__":
    unittest.main()
