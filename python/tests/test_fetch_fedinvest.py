"""Parser tests for fetch_fedinvest.py against a fixture in FedInvest's CSV layout.
Run: python -m unittest discover -s python/tests"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fetch_fedinvest import parse  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
