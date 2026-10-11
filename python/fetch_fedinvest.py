#!/usr/bin/env python3
"""
fetch_fedinvest.py - download US Treasury end-of-day prices into a JSON file.

Where the prices come from
    FedInvest (treasurydirect.gov), run by the US Treasury's Bureau of the Fiscal
    Service, publishes an official end-of-day price for every marketable Treasury
    security each business day. It is free and needs no API key, but it is a web
    form for people rather than an API, so this script fills the form in the same
    way a browser does.

How a fetch works
    1. Load the "Historical Prices" page. It sets a session cookie and contains a
       form with one date field (priceDate, YYYY-MM-DD) and a hidden CSRF token.
    2. Submit that form for a date. The answer is an HTML page with one table row
       per security: CUSIP, type, coupon rate, maturity, call date, buy, sell and
       end-of-day price.
    3. Keep the fixed-coupon notes and bonds (bills, TIPS and FRNs follow other
       conventions) and write them to JSON for the web page and the Python CLI.

    Today (New York time) is tried first, then earlier business days, so weekends,
    holidays and not-yet-published days fall back to the latest prices that exist.
    Every date gets a fresh session and token.

When something goes wrong
    The site must keep working. If no prices can be fetched, the file currently
    deployed on GitHub Pages is reused (marked "carried-over"), and if even that is
    unavailable the existing file is left as it is. The page then shows the data as
    stale instead of breaking. Messages starting with ::warning:: show up as
    warnings in the GitHub Actions run.

Usage
    python fetch_fedinvest.py --out public/data/treasury_prices.json
    python fetch_fedinvest.py --start 2026-10-12      # begin from a given date
    python fetch_fedinvest.py --parse saved.html      # parse a saved result page

Standard library only.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# 1. Settings
# ---------------------------------------------------------------------------
FORM_URL = "https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate"
DEPLOYED_URL = "https://hubertfong.github.io/bond-returns/data/treasury_prices.json"

KEEP_TYPES = {"MARKET BASED NOTE", "MARKET BASED BOND"}   # fixed-coupon securities only
MAX_DAYS_BACK = 7          # business days to try before giving up
SANE_MAX_COUPON = 0.25     # a 25% Treasury coupon means the table format has changed

# The form is meant for browsers, so requests carry ordinary browser headers.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Plain identification for our own GitHub Pages file.
OWN_HEADERS = {"User-Agent": "bond-returns/1.0 (+https://github.com/HubertFong/bond-returns)"}


class LayoutChanged(Exception):
    """The FedInvest page no longer has the form we know how to fill."""


def describe(exc: Exception) -> str:
    """One-line reason for a failed request, including the start of any error page."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = " ".join(exc.read(240).decode("utf-8", errors="replace").split())
        except Exception:
            body = ""
        return f"HTTP {exc.code} {body!r}"
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 2. The form: load it, find the date field, fill it in like a browser
# ---------------------------------------------------------------------------
class FormParser(HTMLParser):
    """Collects every <form> on a page: its action URL and its <input> fields."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._current = {"action": a.get("action", ""), "fields": []}
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None:
            self._current["fields"].append({"type": a.get("type", "text").lower(),
                                            "name": a.get("name", ""), "value": a.get("value", "")})

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def parse_form(html: str) -> dict | None:
    """The price-date form on the page (the one with a date field), or None."""
    parser = FormParser()
    parser.feed(html)
    for form in parser.forms:
        if any(f["type"] == "date" or "date" in f["name"].lower() for f in form["fields"]):
            return form
    return None


def form_payload(form: dict, day: date) -> list[tuple[str, str]]:
    """The fields a browser would send: hidden fields (the CSRF token) as they are,
    the date field set to `day`, and the submit button's name and value."""
    data = []
    for f in form["fields"]:
        if not f["name"]:
            continue
        if f["type"] == "hidden":
            data.append((f["name"], f["value"]))
        elif f["type"] == "date" or "date" in f["name"].lower():
            data.append((f["name"], day.isoformat()))
        elif f["type"] == "submit":
            data.append((f["name"], f["value"]))
    return data


def download(day: date) -> str:
    """Submit the form for one date and return the result page.

    A new cookie jar is used for every call, so each date gets its own session and
    CSRF token (FedInvest answers 403 to a submission without a valid token)."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(urllib.request.Request(FORM_URL, headers=BROWSER_HEADERS), timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    form = parse_form(page)
    if form is None:
        print("::group::FedInvest form page (no date field found)")
        print(page)
        print("::endgroup::")
        raise LayoutChanged("the Historical Prices form has no date field")

    headers = {**BROWSER_HEADERS, "Referer": FORM_URL, "Origin": "https://www.treasurydirect.gov",
               "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode(form_payload(form, day)).encode()
    action = urllib.parse.urljoin(FORM_URL, form["action"] or FORM_URL)
    with opener.open(urllib.request.Request(action, data=body, headers=headers), timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 3. The price table: HTML rows -> clean records
# ---------------------------------------------------------------------------
class TableParser(HTMLParser):
    """The text of every table cell, row by row."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


# Column order as FedInvest prints it; a header row, if present, overrides it.
COLUMNS = ("cusip", "type", "rate", "maturity", "call", "buy", "sell", "eod")
HEADER_NAMES = {"cusip": "cusip", "security type": "type", "rate": "rate", "maturity date": "maturity",
                "call date": "call", "buy": "buy", "sell": "sell", "end of day": "eod"}


def _date(s: str) -> str:
    """'11/15/2034' -> '2034-11-15'."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"bad date {s!r}")


def _price(s: str) -> float | None:
    """A positive price, or None for blanks and zeros (FedInvest prints 0 for 'no price')."""
    try:
        v = float(s.strip())
    except ValueError:
        return None
    return v if v > 0 else None


def parse(html: str) -> list[dict]:
    """Fixed-coupon notes and bonds from a FedInvest result page.

    Each record: cusip, type, rate (decimal coupon), maturity (ISO date), buy, sell
    and eod (end-of-day clean price per 100; the buy/sell mid if eod is missing)."""
    table = TableParser()
    table.feed(html)
    col = {name: i for i, name in enumerate(COLUMNS)}
    found = []
    for row in table.rows:
        names = [HEADER_NAMES.get(c.strip().lower()) for c in row]
        if "cusip" in names and "eod" in names:              # header row: map columns by name
            col = {n: i for i, n in enumerate(names) if n}
            continue
        if len(row) <= max(col.values()):
            continue
        cusip, typ = row[col["cusip"]].strip().upper(), row[col["type"]].strip().upper()
        if len(cusip) != 9 or not cusip.isalnum() or typ not in KEEP_TYPES:
            continue
        buy, sell, eod = _price(row[col["buy"]]), _price(row[col["sell"]]), _price(row[col["eod"]])
        if eod is None and buy and sell:
            eod = round((buy + sell) / 2, 6)
        if eod is None:
            continue
        found.append((row[col["rate"]].strip(), {"cusip": cusip, "type": typ, "rate": None,
                                                  "maturity": _date(row[col["maturity"]]),
                                                  "buy": buy, "sell": sell, "eod": eod}))

    # Coupon units. FedInvest prints percents without a % sign ("4.250"). A low
    # coupon such as 0.125 is ambiguous on its own, so the units are decided for the
    # whole table: percent if any coupon has a % sign or is above 1.
    nums = [float(rate.rstrip("%").replace(",", "")) for rate, _ in found]
    percent = any(rate.endswith("%") for rate, _ in found) or (bool(nums) and max(nums) > 1)
    records = []
    for (_, rec), n in zip(found, nums):
        rec["rate"] = round(n / 100, 10) if percent else n
        records.append(rec)
    if records and max(r["rate"] for r in records) > SANE_MAX_COUPON:
        raise ValueError("RATE column does not look like a coupon; has the FedInvest table changed?")
    return records


# ---------------------------------------------------------------------------
# 4. The fetch loop: latest business day first, then earlier ones
# ---------------------------------------------------------------------------
def us_today() -> date:
    """Today's date in New York, where the Treasury business day is."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:                                   # no time-zone database: close enough
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def business_days_back(start: date, n: int):
    """`start` (if a weekday) and the weekdays before it, newest first, n in total."""
    d = start
    while n > 0:
        if d.weekday() < 5:
            yield d
            n -= 1
        d -= timedelta(days=1)


def fetch_latest(start: date | None = None) -> tuple[date | None, list[dict]]:
    """(price date, records) for the newest date with prices, or (None, [])."""
    refused = 0
    for day in business_days_back(start or us_today(), MAX_DAYS_BACK):
        try:
            page = download(day)
        except LayoutChanged as exc:
            print(f"::warning::FedInvest page layout changed: {exc}")
            break
        except Exception as exc:
            print(f"  {day}: download failed: {describe(exc)}")
            refused += isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 429)
            if refused >= 2:                            # being refused, not missing data
                print("  access refused twice; not trying earlier dates")
                break
            continue
        records = parse(page)
        print(f"  {day}: {len(page)} bytes, {len(records)} notes/bonds")
        if records:
            print("    sample:", *records[:3], sep="\n      ")
            return day, records
        # No table for this date (weekend, holiday or not yet published). The page is
        # logged, folded, so an unexpected answer can be inspected in the Actions run.
        print(f"::group::FedInvest answer for {day} (first 6000 characters)")
        print(page[:6000])
        print("::endgroup::")
    return None, []


# ---------------------------------------------------------------------------
# 5. Output, with the deployed file as a fallback
# ---------------------------------------------------------------------------
def carried_over() -> dict | None:
    """The price file currently on GitHub Pages, relabelled 'carried-over', so a failed
    fetch never replaces good data with nothing."""
    try:
        with urllib.request.urlopen(urllib.request.Request(DEPLOYED_URL, headers=OWN_HEADERS),
                                    timeout=30) as resp:
            data = json.load(resp)
        if data.get("securities") and data.get("status") != "sample":
            data["status"] = "carried-over"
            return data
    except Exception as exc:
        print(f"  deployed file unavailable: {describe(exc)}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Download US Treasury FedInvest end-of-day prices to JSON.")
    ap.add_argument("--out", default="public/data/treasury_prices.json", help="JSON file to write")
    ap.add_argument("--start", type=date.fromisoformat,
                    help="first date to try, YYYY-MM-DD (default: today in New York)")
    ap.add_argument("--parse", metavar="HTML_FILE", help="parse a saved result page and print a summary")
    args = ap.parse_args()

    if args.parse:
        with open(args.parse, encoding="utf-8", errors="replace") as fh:
            records = parse(fh.read())
        print(json.dumps(records[:3], indent=2), f"\n{len(records)} notes/bonds")
        return 0

    print("Fetching FedInvest end-of-day prices")
    day, records = fetch_latest(args.start)
    if records:
        data = {"source": "US Treasury FedInvest end-of-day prices", "status": "fresh",
                "price_date": day.isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "securities": sorted(records, key=lambda r: (r["maturity"], r["cusip"]))}
    else:
        print("::warning::FedInvest fetch failed; carrying over the deployed price file")
        data = carried_over()
        if data is None:
            print("::warning::No deployed price file either; keeping the existing file unchanged")
            return 0
    with open(args.out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"Wrote {len(data['securities'])} securities for {data['price_date']} "
          f"({data['status']}) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
