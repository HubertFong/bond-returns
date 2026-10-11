#!/usr/bin/env python3
"""
fetch_fedinvest.py - download US Treasury FedInvest end-of-day prices into JSON.

FedInvest (treasurydirect.gov) publishes official end-of-day prices for every
marketable Treasury security each business day. It is free and needs no key.
The GitHub Actions workflow runs this before building the site; the front-end
and `bond_returns.py --source fedinvest` both read the JSON it writes.

How: load the "Historical Prices" form (it carries a CSRF token and session
cookie), submit it for a date (priceDate=YYYY-MM-DD) and read the price table
on the result page. Today is tried first, then earlier business days.

Only fixed-coupon notes and bonds are kept (bills, TIPS and FRNs use other
conventions). If FedInvest cannot be reached or parsed, the previously deployed
file is carried over, and failing that the existing file is left alone, so the
site keeps working and shows the data as stale instead of breaking.

Usage:
    python fetch_fedinvest.py --out public/data/treasury_prices.json
    python fetch_fedinvest.py --parse saved.html         # parse a saved result page (or CSV)
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import http.cookiejar
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

FORM = "https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate"
URL = "https://www.treasurydirect.gov/GA-FI/FedInvest/securityPriceDetail"
DEPLOYED = "https://hubertfong.github.io/bond-returns/data/treasury_prices.json"
KEEP_TYPES = {"MARKET BASED NOTE", "MARKET BASED BOND"}
HEADERS = {"User-Agent": "bond-returns/1.0 (+https://github.com/HubertFong/bond-returns)"}
# The FedInvest form is a page for people: fetch it like a browser would (load the
# form first for its session cookies, then submit it with the usual headers).
BROWSER = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
}
_opener = None


def describe(exc: Exception) -> str:
    """One-line reason for a failed request, with the start of an error page."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read(240).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        body = " ".join(body.split())
        return f"HTTP {exc.code} (server: {exc.headers.get('Server', '?')}) {body!r}"
    return f"{type(exc).__name__}: {exc}"


class FormParser(HTMLParser):
    """Collects each <form>: its action and its fields (inputs, selects with options)."""

    def __init__(self):
        super().__init__()
        self.loose = {"action": "", "fields": []}     # fields outside any <form>
        self.forms, self._form, self._select = [], None, None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        target = self._form or self.loose
        if tag == "form":
            self._form = {"action": a.get("action", ""), "fields": []}
            self.forms.append(self._form)
        elif tag in ("input", "button"):
            target["fields"].append({"tag": tag, "type": a.get("type", "text").lower(),
                                     "name": a.get("name", ""), "value": a.get("value", "")})
        elif tag == "select":
            self._select = {"tag": "select", "type": "select", "name": a.get("name", ""), "options": []}
            target["fields"].append(self._select)
        elif tag == "option" and self._select is not None:
            self._select["options"].append(a.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None


def parse_form(html: str):
    """The price-date form on the FedInvest page, or None."""
    p = FormParser()
    p.feed(html)
    for f in p.forms + [p.loose]:
        if any("day" in fld["name"].lower() or "date" in fld["name"].lower() or fld["type"] == "date"
               for fld in f["fields"]):
            return f
    return None


def _pick(options, n: int, width: int) -> str:
    """The option whose number is n ('9' or '09'); else n zero-padded."""
    for o in options:
        if o.strip().isdigit() and int(o) == n:
            return o
    return f"{n:0{width}d}"


def form_payload(form: dict, day: date) -> list:
    """Fill the form like a browser: keep hidden fields (the CSRF token), set the
    date, choose the CSV file type and press the CSV button."""
    data, submit = [], None
    for fld in form["fields"]:
        name, typ, low = fld["name"], fld["type"], fld["name"].lower()
        if not name:
            continue
        if typ == "hidden":
            data.append((name, fld["value"]))
        elif "day" in low:
            data.append((name, _pick(fld.get("options", []), day.day, 2)))
        elif "month" in low:
            data.append((name, _pick(fld.get("options", []), day.month, 2)))
        elif "year" in low:
            data.append((name, _pick(fld.get("options", []), day.year, 4)))
        elif typ == "date" or "date" in low:          # <input type="date">: ISO format
            data.append((name, day.isoformat()))
        elif typ == "radio" and "csv" in fld["value"].lower():
            data.append((name, fld["value"]))
        elif typ == "submit" and (submit is None or "csv" in (fld["value"] + name).lower()):
            submit = (name, fld["value"])
    if submit:
        data.append(submit)
    return data


_form = None


def session(fresh: bool = False) -> urllib.request.OpenerDirector:
    """Cookie-keeping opener; on first use (or when fresh) loads the form page and
    parses its form, so each request carries a new session and CSRF token."""
    global _opener, _form
    if fresh:
        _opener = _form = None
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            with _opener.open(urllib.request.Request(FORM, headers=BROWSER), timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            _form = parse_form(html)
            if _form:
                fields = [f"{f['name']}({f['type']})" for f in _form["fields"] if f["name"]]
                print(f"  form page: {len(html)} bytes; form action={_form['action']!r} fields={fields}")
            else:                                    # show what the page is, to adapt the parser
                print(f"  form page: {len(html)} bytes, no date fields found")
                print("::group::FedInvest form page HTML")
                print(html)
                print("::endgroup::")
        except Exception as exc:
            print(f"  form page: {describe(exc)}")
    return _opener


def us_today() -> date:
    """Today's date in New York, where the Treasury business day is."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:                               # no tz database: close enough
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def business_days_back(start: date, n: int):
    d = start
    while n > 0:
        if d.weekday() < 5:
            yield d
            n -= 1
        d -= timedelta(days=1)


def download(day: date) -> str:
    opener = session(fresh=True)                     # new session + token for every date tried
    if _form:
        data = form_payload(_form, day)
        url = urllib.parse.urljoin(FORM, _form["action"]) if _form["action"] else URL
    else:                                            # page layout unknown: last known field names
        data = [("priceDateDay", f"{day.day:02d}"), ("priceDateMonth", f"{day.month:02d}"),
                ("priceDateYear", str(day.year)), ("fileType", "csv"), ("csv", "CSV FORMAT")]
        url = URL
    headers = {**BROWSER, "Referer": FORM, "Origin": "https://www.treasurydirect.gov",
               "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=headers)
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _date(s: str) -> str:
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"bad date {s!r}")


def _price(s: str):
    s = s.strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


class TableParser(HTMLParser):
    """Text of every <tr>'s cells."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

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


COLUMNS = ("cusip", "type", "rate", "maturity", "call", "buy", "sell", "eod")
HEADER_NAMES = {"cusip": "cusip", "security type": "type", "rate": "rate", "maturity date": "maturity",
                "call date": "call", "buy": "buy", "sell": "sell", "end of day": "eod"}


def rows_of(text: str) -> list:
    if "<table" in text.lower():
        p = TableParser()
        p.feed(text)
        return p.rows
    return list(csv.reader(io.StringIO(text)))


def parse(text: str) -> list:
    """FedInvest prices, from the CSV download or the HTML results table. Columns:
    CUSIP, SECURITY TYPE, RATE, MATURITY DATE, CALL DATE, BUY, SELL, END OF DAY
    (a header row, if present, sets the order). Non-data rows are skipped."""
    col = {name: i for i, name in enumerate(COLUMNS)}
    found = []
    for row in rows_of(text):
        names = [HEADER_NAMES.get(c.strip().lower()) for c in row]
        if "cusip" in names and "eod" in names:          # header row: map columns by name
            col = {n: i for i, n in enumerate(names) if n}
            continue
        if len(row) <= max(col.values()):
            continue
        cusip, typ = row[col["cusip"]].strip().upper(), row[col["type"]].strip().upper()
        if len(cusip) != 9 or not cusip.isalnum() or typ not in KEEP_TYPES:
            continue
        buy, sell, eod = _price(row[col["buy"]]), _price(row[col["sell"]]), _price(row[col["eod"]])
        if eod is None and buy and sell:
            eod = round((buy + sell) / 2, 6)        # mid if the end-of-day column is empty
        if eod is None:
            continue
        found.append((row[col["rate"]].strip(), {"cusip": cusip, "type": typ, "rate": None,
                                                  "maturity": _date(row[col["maturity"]]),
                                                  "buy": buy, "sell": sell, "eod": eod}))
    # Coupon units: '4.250%' is a percent; bare numbers are a percent if any coupon is
    # above 1 (the HTML table), otherwise decimals (the CSV file: 0.04250000).
    nums = [float(r.rstrip("%").replace(",", "")) for r, _ in found]
    percent = any(r.endswith("%") for r, _ in found) or (bool(nums) and max(nums) > 1)
    out = []
    for (_, rec), n in zip(found, nums):
        rec["rate"] = round(n / 100, 10) if percent else n
        out.append(rec)
    if out and max(r["rate"] for r in out) > 0.25:  # a 25% Treasury coupon means a format change
        raise ValueError("RATE column does not look like a coupon; FedInvest format changed?")
    return out


def fetch_latest(start: date | None = None, max_days: int = 7):
    """Try `start` (default today in New York), then earlier business days: holidays
    and not-yet-published days return no rows (or a 400/404) and are skipped."""
    refused = 0
    for day in business_days_back(start or us_today(), max_days):
        try:
            text = download(day)
        except Exception as exc:
            print(f"  {day}: download failed: {describe(exc)}")
            refused += isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 429)
            if refused >= 2:                         # blocked, not missing data: stop asking
                print("  access refused twice; not retrying further dates")
                break
            continue
        rows = parse(text)
        print(f"  {day}: {len(text)} bytes, {len(rows)} notes/bonds")
        if not rows:
            print("::group::FedInvest response (first 6000 characters)")
            print(text[:6000])
            print("::endgroup::")
            continue
        print("    sample rows:", *rows[:3], sep="\n      ")
        return day, rows
    return None, []


def carried_over():
    """The JSON currently deployed, so a failed fetch never replaces good data with nothing."""
    try:
        req = urllib.request.Request(DEPLOYED, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        if data.get("securities") and data.get("status") != "sample":
            data["status"] = "carried-over"
            return data
    except Exception as exc:
        print(f"  deployed file unavailable: {type(exc).__name__}: {exc}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="public/data/treasury_prices.json")
    ap.add_argument("--parse", help="parse a saved FedInvest CSV and print a summary")
    ap.add_argument("--start", type=date.fromisoformat,
                    help="first date to try, YYYY-MM-DD (default: today in New York)")
    args = ap.parse_args()

    if args.parse:
        with open(args.parse) as fh:
            rows = parse(fh.read())
        print(json.dumps(rows[:3], indent=2), f"\n{len(rows)} notes/bonds")
        return 0

    print("Fetching FedInvest end-of-day prices")
    day, rows = fetch_latest(args.start)
    if rows:
        data = {"source": "US Treasury FedInvest end-of-day prices", "status": "fresh",
                "price_date": day.isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "securities": sorted(rows, key=lambda r: (r["maturity"], r["cusip"]))}
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
