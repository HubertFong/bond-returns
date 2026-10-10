#!/usr/bin/env python3
"""
fetch_fedinvest.py - download US Treasury FedInvest end-of-day prices into JSON.

FedInvest (treasurydirect.gov) publishes official end-of-day prices for every
marketable Treasury security each business day. It is free and needs no key.
The GitHub Actions workflow runs this before building the site; the front-end
and `bond_returns.py --source fedinvest` both read the JSON it writes.

Only fixed-coupon notes and bonds are kept (bills, TIPS and FRNs use other
conventions). If FedInvest cannot be reached or parsed, the previously deployed
file is carried over, and failing that the existing file is left alone, so the
site keeps working and shows the data as stale instead of breaking.

Usage:
    python fetch_fedinvest.py --out public/data/treasury_prices.json
    python fetch_fedinvest.py --parse saved.csv          # parse a saved file, print summary
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


def session() -> urllib.request.OpenerDirector:
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            with _opener.open(urllib.request.Request(FORM, headers=BROWSER), timeout=30) as resp:
                print(f"  form page: HTTP {resp.status}, {len(resp.read())} bytes")
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
    form = {"priceDateDay": f"{day.day:02d}", "priceDateMonth": f"{day.month:02d}",
            "priceDateYear": str(day.year), "fileType": "csv", "csv": "CSV FORMAT"}
    headers = {**BROWSER, "Referer": FORM, "Origin": "https://www.treasurydirect.gov",
               "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(URL, data=urllib.parse.urlencode(form).encode(), headers=headers)
    with session().open(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _rate(s: str) -> float:
    """'0.04250000' (decimal) or '4.250%' (percent) -> 0.0425."""
    s = s.strip()
    return float(s.rstrip("%")) / 100 if s.endswith("%") else float(s)


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


def parse(text: str) -> list:
    """FedInvest CSV rows: CUSIP, SECURITY TYPE, RATE, MATURITY DATE, CALL DATE, BUY, SELL, END OF DAY.
    There is no header row; anything that does not look like a data row is skipped."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8:
            continue
        cusip, typ = row[0].strip().upper(), row[1].strip().upper()
        if len(cusip) != 9 or not cusip.isalnum() or typ not in KEEP_TYPES:
            continue
        buy, sell, eod = _price(row[5]), _price(row[6]), _price(row[7])
        if eod is None and buy and sell:
            eod = round((buy + sell) / 2, 6)        # mid if the end-of-day column is empty
        if eod is None:
            continue
        out.append({"cusip": cusip, "type": typ, "rate": _rate(row[2]), "maturity": _date(row[3]),
                    "buy": buy, "sell": sell, "eod": eod})
    if out and max(r["rate"] for r in out) > 0.25:  # a 25% Treasury coupon means a format change
        raise ValueError("RATE column does not look like a decimal coupon; FedInvest format changed?")
    return out


def fetch_latest(max_days: int = 7):
    """Try today, then earlier business days (holidays, not yet published)."""
    refused = 0
    for day in business_days_back(us_today(), max_days):
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
            print("    first bytes:", text[:300].replace("\n", " | "))
            continue
        print("    sample rows:", *text.strip().splitlines()[:3], sep="\n      ")
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
    args = ap.parse_args()

    if args.parse:
        with open(args.parse) as fh:
            rows = parse(fh.read())
        print(json.dumps(rows[:3], indent=2), f"\n{len(rows)} notes/bonds")
        return 0

    print("Fetching FedInvest end-of-day prices")
    day, rows = fetch_latest()
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
