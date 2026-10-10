#!/usr/bin/env python3
"""
bond_returns.py - live return metrics for a generic fixed-coupon bond.

Computes, every N seconds from a live clean price (or yield):
    * accrued interest, clean and dirty price
    * current yield
    * yield to maturity (Newton-Raphson, falling back to scipy brentq)
    * holding-period return since purchase (price change + accrued + coupons),
      plus its annualised equivalent

Conventions (see README.md in this folder for the formulas and a worked example):
    * Prices are quoted per 100 of face value; yields are decimals (0.065 = 6.5%).
    * YTM is the "street" yield: compounded at the coupon frequency, with the first
      period discounted for the fractional period w = DSC / E (Excel PRICE/YIELD,
      SIA, ICMA). When one cash flow is left, simple interest is used for it
      (the Excel/SIA rule); set simple_last_period=False to keep compounding.
    * Regular coupon schedule rolled back from maturity (no odd first/last coupons),
      no ex-coupon period, weekend-only business-day calendar.

Dependencies:
    Python >= 3.9
    scipy >= 1.7          pip install scipy   (brentq fallback; a bisection fallback
                                               is used if scipy is not installed)
    Optional, live data only:
    blpapi                Bloomberg Desktop/Server API (Terminal or B-PIPE login)
    lseg-data             LSEG/Refinitiv Data Library (or the older refinitiv-data)
    (the free "fedinvest" source needs nothing extra)

Usage:
    python bond_returns.py --validate                 # validation + edge-case tests
    python bond_returns.py --example                  # hand worked example, step by step
    python bond_returns.py --source mock --interval 1 --iterations 10
    python bond_returns.py --source fedinvest --ticker 91282CLW9 --interval 300
    python bond_returns.py --config my_bond.json --source bloomberg \
        --ticker "US91282CJZ59 Govt" --interval 15
"""
from __future__ import annotations

import argparse
import calendar
import json
import logging
import math
import random
import sys
import time
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

try:
    from scipy.optimize import brentq
except ImportError:  # the script still runs; _bisect() is used instead
    brentq = None

log = logging.getLogger("bond_returns")

DAY_COUNTS = ("30/360", "ACT/ACT", "ACT/365")


# ---------------------------------------------------------------------------
# 1. Configuration (everything bond-specific lives here, nothing hard-coded)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Bond:
    face_value: float = 1000.0         # currency amount per bond; prices are per 100
    coupon_rate: float = 0.06          # annual coupon, decimal; 0.0 for a zero-coupon bond
    frequency: int = 2                 # coupons per year: 1, 2, 4 or 12 (compounding for zeros)
    maturity: date = date(2029, 6, 15)
    day_count: str = "30/360"          # "30/360" (US), "ACT/ACT" (ICMA) or "ACT/365" (Fixed)
    redemption: float = 100.0          # redemption per 100 face
    eom_rule: bool = True              # if maturity is month-end, keep every coupon month-end
    simple_last_period: bool = True    # Excel/SIA: simple interest when one cash flow is left

    def __post_init__(self):
        if self.frequency not in (1, 2, 4, 12):
            raise ValueError("frequency must be 1, 2, 4 or 12")
        if self.day_count not in DAY_COUNTS:
            raise ValueError(f"day_count must be one of {DAY_COUNTS}")
        if self.coupon_rate < 0 or self.face_value <= 0 or self.redemption <= 0:
            raise ValueError("coupon_rate must be >= 0; face_value and redemption > 0")

    @property
    def coupon_per_period(self) -> float:   # per 100 face
        return 100.0 * self.coupon_rate / self.frequency


@dataclass(frozen=True)
class Position:
    purchase_settle: date = date(2025, 9, 15)  # settlement date of the purchase
    purchase_clean: float = 97.00              # clean price paid, per 100


@dataclass(frozen=True)
class FeedConfig:
    source: str = "mock"               # "mock" | "fedinvest" | "bloomberg" | "refinitiv"
    ticker: str = "US0000000000 Corp"  # Bloomberg security or Refinitiv RIC
    field: str = ""                    # blank -> PX_LAST (Bloomberg) / CF_LAST (Refinitiv)
    kind: str = "price"                # "price" (clean, per 100) or "yield" (decimal)
    interval_s: float = 5.0            # polling interval
    stale_after_s: float = 120.0       # quote older than this -> flagged STALE
    max_reuse_s: float = 900.0         # reuse last good quote for at most this long
    max_jump_pct: float = 5.0          # reject a tick that moves more than this...
    max_rejects: int = 3               # ...unless it repeats this many times in a row
    settle_lag_days: int = 1           # T+1 (US Treasuries); 2 for most corporates/Bunds
    settle_date: Optional[date] = None  # fixed settlement date overrides the lag
    mock_start: float = 98.50          # mock feed: first clean price (kind="price")
    mock_start_yield: float = 0.0662   # mock feed: first yield (kind="yield")
    mock_seed: int = 42
    data_url: str = "https://hubertfong.github.io/bond-returns/data/treasury_prices.json"


# ---------------------------------------------------------------------------
# 2. Dates, schedule and day counts
# ---------------------------------------------------------------------------
def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def add_months(d: date, months: int, month_end: bool = False) -> date:
    """Shift d by whole months, clipping to the month's last day (or forcing it)."""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, last if month_end else min(d.day, last))


def add_business_days(d: date, n: int) -> date:
    """Weekends only; plug a holiday calendar in here for production use."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def coupon_dates(bond: Bond, after: date) -> Tuple[date, List[date]]:
    """Return (last coupon date <= after, [coupon dates > after, ..., maturity]).

    Dates are rolled back from maturity (each one from maturity itself, so a 31st
    does not drift to the 30th). If `after` is a coupon date, that coupon belongs to
    the seller: it becomes the previous date and is excluded from future flows.
    """
    if after >= bond.maturity:
        raise ValueError(f"bond matured on {bond.maturity}; settlement {after}")
    step = 12 // bond.frequency
    month_end = bond.eom_rule and _is_month_end(bond.maturity)
    future, k = [], 0
    while True:
        d = add_months(bond.maturity, -k * step, month_end)
        if d <= after:
            return d, future[::-1]
        future.append(d)
        k += 1


def days_30_360(d1: date, d2: date, month_end_bond: bool = False) -> int:
    """30/360 US (SIA / Excel basis 0). February rules only for month-end bonds."""
    y1, m1, dd1 = d1.year, d1.month, d1.day
    y2, m2, dd2 = d2.year, d2.month, d2.day
    feb_end = lambda d: d.month == 2 and _is_month_end(d)  # noqa: E731
    if month_end_bond and feb_end(d1) and feb_end(d2):
        dd2 = 30
    if month_end_bond and feb_end(d1):
        dd1 = 30
    if dd2 == 31 and dd1 >= 30:
        dd2 = 30
    if dd1 == 31:
        dd1 = 30
    return 360 * (y2 - y1) + 30 * (m2 - m1) + (dd2 - dd1)


@dataclass(frozen=True)
class Cashflow:
    pay_date: date
    coupon: float      # per 100 face
    principal: float   # per 100 face

    @property
    def amount(self) -> float:
        return self.coupon + self.principal


@dataclass(frozen=True)
class SettleState:
    """Everything about the bond that depends only on the settlement date."""
    settle: date
    prev_coupon: date
    next_coupon: date
    flows: Tuple[Cashflow, ...]
    A: float          # accrued days: last coupon -> settlement
    E: float          # days in the coupon period
    DSC: float        # days: settlement -> next coupon
    accrued: float    # accrued interest per 100

    @property
    def w(self) -> float:  # fraction of a period to the next coupon
        return self.DSC / self.E


def settle_state(bond: Bond, settle: date) -> SettleState:
    """Cash-flow schedule, day-count terms and accrued interest at `settle`."""
    prev, future = coupon_dates(bond, settle)
    nxt, f = future[0], bond.frequency
    if bond.day_count == "30/360":
        E = 360.0 / f
        A = float(days_30_360(prev, settle, bond.eom_rule and _is_month_end(bond.maturity)))
        DSC = E - A
    elif bond.day_count == "ACT/ACT":          # ICMA: actual days in the actual period
        E = float((nxt - prev).days)
        A = float((settle - prev).days)
        DSC = float((nxt - settle).days)
    else:                                      # ACT/365 Fixed
        E = 365.0 / f
        A = float((settle - prev).days)
        DSC = float((nxt - settle).days)
    c = bond.coupon_per_period
    flows = tuple(Cashflow(d, c, bond.redemption if d == bond.maturity else 0.0)
                  for d in future)
    return SettleState(settle, prev, nxt, flows, A, E, DSC, accrued=c * A / E)


def coupons_received(bond: Bond, start: date, end: date) -> float:
    """Coupons (per 100) paid in (start, end]: owned on the record date."""
    if end <= start:
        return 0.0
    step = 12 // bond.frequency
    month_end = bond.eom_rule and _is_month_end(bond.maturity)
    total, k = 0.0, 0
    while True:
        d = add_months(bond.maturity, -k * step, month_end)
        if d <= start:
            return total
        if d <= end:
            total += bond.coupon_per_period
        k += 1


# ---------------------------------------------------------------------------
# 3. Pricing and the YTM solver
# ---------------------------------------------------------------------------
def dirty_price(bond: Bond, st: SettleState, y: float) -> Tuple[float, float]:
    """Dirty price per 100 at yield y, and dP/dy (used by Newton-Raphson).

        P_dirty = sum_k CF_k / (1 + y/f)^(w + k - 1),   k = 1..N
    """
    f, w = bond.frequency, st.w
    if bond.simple_last_period and len(st.flows) == 1:
        cf = st.flows[0].amount
        denom = 1.0 + w * y / f
        if denom <= 0:
            raise ValueError("yield below the -f/w limit")
        return cf / denom, -cf * (w / f) / denom ** 2
    base = 1.0 + y / f
    if base <= 0:
        raise ValueError("yield at or below -frequency")
    pv = dpv = 0.0
    for k, cf in enumerate(st.flows):
        t = w + k                        # exponent in coupon periods
        df = base ** (-t)
        pv += cf.amount * df
        dpv -= cf.amount * (t / f) * df / base
    return pv, dpv


def clean_price(bond: Bond, st: SettleState, y: float) -> float:
    return dirty_price(bond, st, y)[0] - st.accrued


def approx_ytm(bond: Bond, st: SettleState, clean: float) -> float:
    """Textbook start value: (annual coupon + pull to par per year) / average price."""
    years = max((bond.maturity - st.settle).days / 365.25, 1 / 365.25)
    y = (100 * bond.coupon_rate + (bond.redemption - clean) / years) / ((bond.redemption + clean) / 2)
    return min(max(y, -0.5 * bond.frequency), 1.0)


def _bisect(g: Callable[[float], float], lo: float, hi: float, tol: float = 1e-15) -> float:
    """Plain bisection, only used when scipy is missing. g(lo) > 0 > g(hi)."""
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        if g(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def solve_ytm(bond: Bond, st: SettleState, clean: float, guess: Optional[float] = None,
              tol: float = 1e-11, trace: Optional[list] = None) -> Tuple[float, str, int]:
    """Solve P_dirty(y) = clean + accrued for y. Returns (ytm, method, iterations).

    Newton-Raphson first (2-4 iterations from a good guess, 1 from the previous tick);
    if it diverges, leaves the domain y > -f or stalls, fall back to brentq on a
    bracket. The price is strictly decreasing in y, so a positive price always has
    exactly one root - including negative yields when price > sum of cash flows.
    """
    if not (math.isfinite(clean) and clean > 0):
        raise ValueError(f"invalid clean price {clean!r}")
    target = clean + st.accrued
    # Domain: 1 + y/f > 0, or 1 + w*y/f > 0 when the last period uses simple interest.
    simple = bond.simple_last_period and len(st.flows) == 1
    floor = (-bond.frequency / st.w if simple and st.w > 0 else -bond.frequency) + 1e-9
    g = lambda y: dirty_price(bond, st, y)[0] - target    # noqa: E731

    y = guess if (guess is not None and math.isfinite(guess) and guess > floor) \
        else approx_ytm(bond, st, clean)
    for i in range(1, 51):
        p, dp = dirty_price(bond, st, y)
        diff = p - target
        if trace is not None:
            trace.append((i, y, p, dp, diff))
        if abs(diff) < tol:
            return y, "newton", i
        if not (math.isfinite(dp) and dp < 0):
            break
        y_next = y - diff / dp
        if not math.isfinite(y_next) or y_next <= floor:
            break                                          # would leave the domain
        y = y_next

    # Fallback: bracket the root, then brentq (or bisection without scipy).
    lo, hi = -0.01, 0.10
    for _ in range(200):                  # g(lo) must be > 0: walk towards the floor
        if lo > floor and g(lo) > 0:
            break
        lo = floor + (max(lo, floor) - floor) / 2
    for _ in range(100):                  # g(hi) must be < 0: widen upwards
        if g(hi) < 0:
            break
        hi = 2 * hi + 0.1
    if not (g(lo) > 0 > g(hi)):
        raise ValueError(f"could not bracket a yield for clean price {clean}")
    if brentq is not None:
        root, res = brentq(g, lo, hi, xtol=1e-15, rtol=4 * sys.float_info.epsilon,
                           maxiter=500, full_output=True)
        return root, "brentq", res.iterations
    return _bisect(g, lo, hi), "bisection", 0


# ---------------------------------------------------------------------------
# 4. Return metrics
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    settle: date
    clean: float
    accrued: float
    dirty: float
    market_value: float              # dirty value of one bond, in currency
    current_yield: Optional[float]
    ytm: float
    solver: str
    iterations: int
    coupons_received: float          # per 100, since purchase
    holding_days: int
    hpr: Optional[float]             # holding-period return, not annualised
    hpr_annualised: Optional[float]  # compound, ACT/365


def compute_metrics(bond: Bond, pos: Position, settle: date, clean: float,
                    ytm_guess: Optional[float] = None) -> Metrics:
    st = settle_state(bond, settle)
    dirty = clean + st.accrued
    ytm, method, iters = solve_ytm(bond, st, clean, guess=ytm_guess)

    # Current yield: annual coupon / clean price (zero for a zero-coupon bond).
    cy = (100 * bond.coupon_rate / clean) if clean > 0 else None

    # Holding-period return on the dirty price actually paid:
    #   HPR = (P_dirty,now + coupons received - P_dirty,purchase) / P_dirty,purchase
    hpr = ann = None
    days = (settle - pos.purchase_settle).days
    cpn = coupons_received(bond, pos.purchase_settle, settle)
    if days >= 0:
        paid = pos.purchase_clean + settle_state(bond, pos.purchase_settle).accrued
        hpr = (dirty + cpn - paid) / paid
        if days > 0 and hpr > -1:
            ann = (1 + hpr) ** (365.0 / days) - 1
    return Metrics(settle, clean, st.accrued, dirty, dirty / 100 * bond.face_value,
                   cy, ytm, method, iters, cpn, days, hpr, ann)


# ---------------------------------------------------------------------------
# 5. Live data: everything goes through get_live_price()
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Quote:
    value: float           # clean price per 100, or yield (decimal), per `kind`
    kind: str              # "price" | "yield"
    timestamp: datetime    # source time of the quote (UTC); receive time if unknown
    source: str


class MockFeed:
    """Random walk with occasional missing and stale ticks, for testing the loop."""

    def __init__(self, start: float, kind: str = "price", seed: int = 42,
                 p_missing: float = 0.10, p_stale: float = 0.10):
        self.value, self.kind = start, kind
        self.rng = random.Random(seed)
        self.p_missing, self.p_stale = p_missing, p_stale
        self.first = True

    def __call__(self) -> Optional[Quote]:
        now = datetime.now(timezone.utc)
        if self.first:                       # first tick: exactly the start value
            self.first = False
            return Quote(self.value, self.kind, now, "mock")
        u = self.rng.random()
        if u < self.p_missing:
            return None                      # feed returned nothing
        self.value += self.rng.gauss(0, 0.02 if self.kind == "price" else 0.0002)
        if u < self.p_missing + self.p_stale:
            return Quote(self.value, self.kind, now - timedelta(minutes=10), "mock")
        return Quote(self.value, self.kind, now, "mock")


_mock_feed: Optional[MockFeed] = None
_bbg_session = None
_lseg = None


def _bloomberg_quote(feed: FeedConfig) -> Optional[Quote]:
    """Bloomberg reference-data snapshot via blpapi. UNTESTED TEMPLATE - check the
    security string and fields (FLDS<GO>) against your terminal/B-PIPE entitlements.
    For tick-by-tick data subscribe to //blp/mktdata instead of polling."""
    global _bbg_session
    import blpapi                                      # optional dependency
    if _bbg_session is None:
        _bbg_session = blpapi.Session()                # localhost:8194 by default
        if not _bbg_session.start() or not _bbg_session.openService("//blp/refdata"):
            _bbg_session = None
            raise ConnectionError("cannot start Bloomberg session")
    fld = feed.field or ("PX_LAST" if feed.kind == "price" else "YLD_YTM_MID")
    req = _bbg_session.getService("//blp/refdata").createRequest("ReferenceDataRequest")
    req.getElement("securities").appendValue(feed.ticker)
    for name in (fld, "LAST_UPDATE_DT", "LAST_UPDATE"):
        req.getElement("fields").appendValue(name)
    _bbg_session.sendRequest(req)
    received = datetime.now(timezone.utc)
    while True:
        ev = _bbg_session.nextEvent(5000)              # ms
        if ev.eventType() == blpapi.Event.TIMEOUT:
            return None
        for msg in ev:
            if not msg.hasElement("securityData"):
                continue
            sec = msg.getElement("securityData").getValueAsElement(0)
            if sec.hasElement("securityError"):
                raise LookupError(str(sec.getElement("securityError")))
            fd = sec.getElement("fieldData")
            if not fd.hasElement(fld):
                return None                            # no value: treated as missing
            value = fd.getElementAsFloat(fld)
            if feed.kind == "yield":
                value /= 100.0                         # Bloomberg yields are in percent
            ts = received
            try:   # LAST_UPDATE_DT + LAST_UPDATE are in the terminal's local time zone
                d = fd.getElement("LAST_UPDATE_DT").getValue()
                t = fd.getElement("LAST_UPDATE").getValue()
                ts = datetime.combine(d, t).astimezone(timezone.utc)
            except Exception:
                pass
            return Quote(value, feed.kind, ts, "bloomberg")
        if ev.eventType() == blpapi.Event.RESPONSE:
            return None


def _refinitiv_quote(feed: FeedConfig) -> Optional[Quote]:
    """LSEG/Refinitiv snapshot via the Data Library. UNTESTED TEMPLATE - needs an
    open Workspace/Eikon desktop session or platform credentials."""
    global _lseg
    if _lseg is None:
        try:
            import lseg.data as ld                     # current package
        except ImportError:
            import refinitiv.data as ld                # older package, same API
        ld.open_session()
        _lseg = ld
    fld = feed.field or ("CF_LAST" if feed.kind == "price" else "RED_YIELD")
    df = _lseg.get_data(universe=[feed.ticker], fields=[fld, "CF_DATE", "CF_TIME"])
    received = datetime.now(timezone.utc)
    if df is None or df.empty or fld not in df.columns:
        return None
    value = df[fld].iloc[0]
    if value is None or not math.isfinite(float(value)):
        return None
    value = float(value) / (100.0 if feed.kind == "yield" else 1.0)
    ts = received
    try:   # CF_DATE / CF_TIME are the exchange/contributor time, in GMT
        ts = datetime.fromisoformat(f"{df['CF_DATE'].iloc[0]}T{df['CF_TIME'].iloc[0]}") \
            .replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return Quote(value, feed.kind, ts, "refinitiv")


def treasury_row(feed: FeedConfig) -> Tuple[Optional[dict], str]:
    """(row for CUSIP feed.ticker, price date) from the FedInvest JSON that
    fetch_fedinvest.py writes and the GitHub Pages site serves (URL or local path)."""
    from urllib.request import urlopen
    if feed.data_url.startswith(("http://", "https://")):
        with urlopen(f"{feed.data_url}?t={int(time.time())}", timeout=20) as resp:
            data = json.load(resp)
    else:
        with open(feed.data_url) as fh:
            data = json.load(fh)
    row = next((r for r in data["securities"] if r["cusip"] == feed.ticker.strip().upper()), None)
    return row, data["price_date"]


def _fedinvest_quote(feed: FeedConfig) -> Optional[Quote]:
    """Free US Treasury end-of-day clean price. One value per business day, so the
    quote's age is hours, not seconds: raise stale_after_s accordingly."""
    row, price_date = treasury_row(feed)
    if row is None or not row.get("eod"):
        return None
    # FedInvest prices are struck early afternoon New York time; 13:00 ET ~ 17:00/18:00 UTC.
    ts = datetime.fromisoformat(price_date).replace(hour=18, tzinfo=timezone.utc)
    return Quote(float(row["eod"]), "price", ts, "fedinvest")


def get_live_price(feed: FeedConfig) -> Optional[Quote]:
    """THE single entry point for market data. Returns a Quote, or None when the
    source has no value. Swap the source with feed.source, or replace this
    function (or pass fetch=... to LiveMonitor) to inject a test feed."""
    global _mock_feed
    if feed.source == "mock":
        if _mock_feed is None:
            start = feed.mock_start if feed.kind == "price" else feed.mock_start_yield
            _mock_feed = MockFeed(start, feed.kind, feed.mock_seed)
        return _mock_feed()
    if feed.source == "fedinvest":
        return _fedinvest_quote(feed)
    if feed.source == "bloomberg":
        return _bloomberg_quote(feed)
    if feed.source == "refinitiv":
        return _refinitiv_quote(feed)
    raise ValueError(f"unknown source {feed.source!r}")


# ---------------------------------------------------------------------------
# 6. Real-time loop: fetch -> check -> recompute -> log
# ---------------------------------------------------------------------------
class LiveMonitor:
    def __init__(self, bond: Bond, pos: Position, feed: FeedConfig,
                 fetch: Callable[[FeedConfig], Optional[Quote]] = get_live_price,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.bond, self.pos, self.feed = bond, pos, feed
        self.fetch, self.clock = fetch, clock
        self.last_good: Optional[Tuple[Quote, float]] = None  # (quote, its clean price)
        self.last_ytm: Optional[float] = None                 # warm start for Newton
        self.rejects = 0

    def settlement(self, now: datetime) -> date:
        if self.feed.settle_date:
            return self.feed.settle_date
        return add_business_days(now.astimezone().date(), self.feed.settle_lag_days)

    def _to_clean(self, q: Quote, settle: date) -> float:
        if q.kind == "price":
            return q.value
        return clean_price(self.bond, settle_state(self.bond, settle), q.value)

    def _problem(self, q: Quote, clean: float, now: datetime) -> Optional[str]:
        """Return why a quote is unusable, or None if it passes the checks."""
        if not (math.isfinite(q.value) and math.isfinite(clean)):
            return "non-numeric value"
        if not 0 < clean < 1000:
            return f"clean price {clean:.4f} out of range"
        if q.timestamp > now + timedelta(seconds=5):
            return "timestamp in the future (clock skew?)"
        if self.last_good is not None:
            prev = self.last_good[1]
            if abs(clean / prev - 1) * 100 > self.feed.max_jump_pct:
                self.rejects += 1
                if self.rejects < self.feed.max_rejects:
                    return f"jump {prev:.4f} -> {clean:.4f} (reject {self.rejects})"
        self.rejects = 0
        return None

    def step(self) -> Tuple[str, Optional[Metrics]]:
        now = self.clock()
        settle = self.settlement(now)
        status, clean, q = "OK", None, None
        try:
            q = self.fetch(self.feed)
        except Exception as exc:                 # feed down, timeout, bad ticker...
            log.warning("feed error: %s: %s", type(exc).__name__, exc)
        if q is not None:
            try:
                clean = self._to_clean(q, settle)
                problem = self._problem(q, clean, now)
            except ValueError as exc:
                problem = str(exc)
            if problem:
                log.warning("rejected quote %r: %s", q.value, problem)
                q = None
            else:
                self.last_good = (q, clean)
        if q is None:                            # missing or rejected: reuse if fresh enough
            if self.last_good is None:
                log.warning("NO DATA: no quote received yet")
                return "NO DATA", None
            q, clean = self.last_good
            if (now - q.timestamp).total_seconds() > self.feed.max_reuse_s:
                log.warning("NO DATA: last good quote is too old (%s)", q.timestamp.isoformat())
                return "NO DATA", None
            status = "REUSED"
            if q.kind == "yield":                # re-price at the current settlement date
                clean = self._to_clean(q, settle)
        age = max(0.0, (now - q.timestamp).total_seconds())
        if age > self.feed.stale_after_s:
            status = "STALE" if status == "OK" else status + "+STALE"

        m = compute_metrics(self.bond, self.pos, settle, clean, self.last_ytm)
        self.last_ytm = m.ytm
        pct = lambda x: "n/a" if x is None else f"{100 * x:.4f}%"  # noqa: E731
        log.info("%-12s px %9.4f age %6.1fs | settle %s AI %.4f dirty %9.4f | "
                 "CY %s YTM %s (%s/%d) | HPR %s ann %s (%dd)",
                 status, m.clean, age, m.settle, m.accrued, m.dirty, pct(m.current_yield),
                 pct(m.ytm), m.solver, m.iterations, pct(m.hpr), pct(m.hpr_annualised),
                 m.holding_days)
        return status, m

    def run(self, iterations: Optional[int] = None) -> None:
        next_t, n = time.monotonic(), 0
        try:
            while iterations is None or n < iterations:
                try:
                    self.step()
                except ValueError as exc:        # e.g. bond matured: nothing left to do
                    log.error("stopping: %s", exc)
                    return
                except Exception:                # a bug must not kill a live monitor
                    log.exception("unexpected error; continuing")
                n += 1
                next_t += self.feed.interval_s   # fixed cadence, no drift
                time.sleep(max(0.0, next_t - time.monotonic()))
        except KeyboardInterrupt:
            log.info("stopped by user")


# ---------------------------------------------------------------------------
# 7. Worked example and validation
# ---------------------------------------------------------------------------
def worked_example() -> None:
    bond, pos = Bond(), Position()
    settle, clean = date(2026, 10, 12), 98.50
    st = settle_state(bond, settle)
    print(f"Bond: {100 * bond.coupon_rate:.2f}% coupon, f={bond.frequency}, maturity "
          f"{bond.maturity}, {bond.day_count}; settlement {settle}, clean {clean}")
    print(f"\n1) Cash-flow schedule (per 100), w = DSC/E = {st.DSC:g}/{st.E:g} = {st.w:.4f}")
    print(f"   {'date':<12}{'CF':>8}{'t (periods)':>13}")
    for k, cf in enumerate(st.flows):
        print(f"   {cf.pay_date!s:<12}{cf.amount:>8.2f}{st.w + k:>13.4f}")
    print(f"\n2) Accrued: last coupon {st.prev_coupon}, A = {st.A:g} days, E = {st.E:g}")
    print(f"   AI = {bond.coupon_per_period:g} x {st.A:g}/{st.E:g} = {st.accrued:.4f}")
    print(f"   Dirty = {clean} + {st.accrued:.4f} = {clean + st.accrued:.4f}")
    y0 = approx_ytm(bond, st, clean)
    print(f"\n3) YTM. Start value (approximation formula) y0 = {100 * y0:.4f}%")
    trace: list = []
    y, method, n = solve_ytm(bond, st, clean, trace=trace)
    print(f"   {'it':>3}{'y':>12}{'P_dirty(y)':>14}{'dP/dy':>12}{'P - target':>14}")
    for i, yi, p, dp, diff in trace:
        print(f"   {i:>3}{100 * yi:>11.6f}%{p:>14.6f}{dp:>12.4f}{diff:>14.2e}")
    print(f"   YTM = {100 * y:.6f}% ({method}, {n} iterations)")
    m = compute_metrics(bond, pos, settle, clean)
    paid = pos.purchase_clean + settle_state(bond, pos.purchase_settle).accrued
    print(f"\n4) Current yield = {100 * bond.coupon_rate:g} / {clean} = {100 * m.current_yield:.4f}%")
    print(f"\n5) Holding period {pos.purchase_settle} -> {settle} ({m.holding_days} days)")
    print(f"   paid dirty {paid:.4f}, now dirty {m.dirty:.4f}, coupons {m.coupons_received:.2f}")
    print(f"   HPR = ({m.dirty:.4f} + {m.coupons_received:.2f} - {paid:.4f}) / {paid:.4f}"
          f" = {100 * m.hpr:.4f}%")
    print(f"   annualised = (1 + HPR)^(365/{m.holding_days}) - 1 = {100 * m.hpr_annualised:.4f}%")


def run_validation() -> bool:
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, bool(ok), detail))

    # (a) Microsoft Excel YIELD() documentation example -> 6.5%
    b = Bond(coupon_rate=0.0575, frequency=2, maturity=date(2016, 11, 15), day_count="30/360")
    st = settle_state(b, date(2008, 2, 15))
    y, meth, _ = solve_ytm(b, st, 95.04287)
    check("Excel YIELD example", abs(y - 0.065) < 1e-6, f"{100 * y:.6f}% vs 6.500000% ({meth})")

    # (b) Microsoft Excel PRICE() documentation example -> 94.63436
    b = Bond(coupon_rate=0.0575, frequency=2, maturity=date(2017, 11, 15), day_count="30/360")
    p = clean_price(b, settle_state(b, date(2008, 2, 15)), 0.065)
    check("Excel PRICE example", abs(p - 94.63436) < 5e-6, f"{p:.5f} vs 94.63436")

    # (c) the hand worked example
    m = compute_metrics(Bond(), Position(), date(2026, 10, 12), 98.50)
    check("Worked example AI/HPR", abs(m.accrued - 1.95) < 1e-12 and abs(m.hpr - 7.95 / 98.5) < 1e-12,
          f"AI {m.accrued:.4f}, HPR {100 * m.hpr:.4f}%")

    # (d) price -> yield -> price round trip, every day count, incl. negative yields
    worst = 0.0
    for dc in DAY_COUNTS:
        for f in (1, 2, 4, 12):
            b = Bond(coupon_rate=0.045, frequency=f, maturity=date(2034, 3, 31), day_count=dc)
            for settle in (date(2026, 10, 12), date(2033, 12, 1)):
                st = settle_state(b, settle)
                for y_true in (-0.02, -0.005, 0.0, 0.03, 0.12, 0.40):
                    y, _, _ = solve_ytm(b, st, clean_price(b, st, y_true))
                    worst = max(worst, abs(y - y_true))
    check("Round trip (3 day counts x 4 freqs)", worst < 1e-10, f"max |dy| = {worst:.1e}")

    # (e) zero-coupon bond: closed form 100 / (1 + y/2)^(2T) on ACT/ACT
    b = Bond(coupon_rate=0.0, frequency=2, maturity=date(2031, 5, 15), day_count="ACT/ACT")
    st = settle_state(b, date(2026, 11, 15))           # on a period date: t = 9 periods
    p = 100 / (1 + 0.04 / 2) ** 9
    y, _, _ = solve_ytm(b, st, p)
    cy = compute_metrics(b, Position(date(2026, 11, 15), p), st.settle, p).current_yield
    check("Zero-coupon", abs(y - 0.04) < 1e-12 and st.accrued == 0 and cy == 0,
          f"YTM {100 * y:.6f}%, AI {st.accrued}, CY {cy}")

    # (f) settlement on a coupon date: no accrued, full period to next coupon,
    #     and today's coupon goes to the seller
    b = Bond()
    st = settle_state(b, date(2026, 12, 15))
    check("Settle on coupon date", st.accrued == 0 and st.w == 1
          and st.flows[0].pay_date == date(2027, 6, 15),
          f"AI {st.accrued}, w {st.w}, first flow {st.flows[0].pay_date}")
    check("Coupon on purchase date not received",
          coupons_received(b, date(2025, 12, 15), date(2026, 6, 15)) == 3.0, "(s, e] rule")

    # (g) negative yield: price above the sum of remaining cash flows
    b = Bond(coupon_rate=0.005, frequency=1, maturity=date(2028, 10, 12), day_count="ACT/ACT")
    st = settle_state(b, date(2026, 10, 12))
    y, meth, _ = solve_ytm(b, st, 102.0)
    check("Negative yield", y < 0 and abs(clean_price(b, st, y) - 102.0) < 1e-9,
          f"YTM {100 * y:.4f}% ({meth})")

    # (h) last period: Excel/SIA simple interest vs compounding
    b = Bond(coupon_rate=0.05, maturity=date(2027, 3, 15))
    st = settle_state(b, date(2026, 10, 12))
    ys, _, _ = solve_ytm(b, st, 100.5)
    yc, _, _ = solve_ytm(replace(b, simple_last_period=False), st, 100.5)
    check("Last period simple vs compound", len(st.flows) == 1 and ys != yc,
          f"simple {100 * ys:.4f}% / compound {100 * yc:.4f}%")
    # simple interest allows yields below -f (domain is y > -f/w): very rich short bond
    b = Bond(coupon_rate=0.0, frequency=1, maturity=date(2027, 1, 31), day_count="ACT/ACT")
    st = settle_state(b, date(2026, 12, 15))
    y, meth, _ = solve_ytm(b, st, 131.0)
    check("Last period, yield below -f", y < -b.frequency and abs(clean_price(b, st, y) - 131.0) < 1e-9,
          f"{100 * y:.2f}% ({meth})")

    # (h2) Newton from a hopeless start value -> bracketed brentq/bisection still converges
    b = Bond(maturity=date(2056, 6, 15))
    st = settle_state(b, date(2026, 10, 12))
    y, meth, _ = solve_ytm(b, st, 98.5, guess=50.0)
    check("Newton -> fallback", meth != "newton" and abs(clean_price(b, st, y) - 98.5) < 1e-9,
          f"{meth}: {100 * y:.6f}%")

    # (i) month-end schedule does not drift (31st stays 31st, Feb end stays Feb end)
    b = Bond(maturity=date(2030, 8, 31))
    prev, fut = coupon_dates(b, date(2026, 10, 12))
    check("Month-end schedule", prev == date(2026, 8, 31) and fut[0] == date(2027, 2, 28),
          f"prev {prev}, next {fut[0]}")

    # (j) matured bond is refused
    try:
        settle_state(Bond(), date(2029, 6, 15))
        check("Matured bond refused", False, "no error")
    except ValueError:
        check("Matured bond refused", True, "ValueError")

    # (k) live loop data handling with a scripted feed
    t0 = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
    clock = {"now": t0}
    feed = FeedConfig(settle_date=date(2026, 10, 12), stale_after_s=60, max_reuse_s=300)
    script = [Quote(98.5, "price", t0, "t"), None,
              Quote(float("nan"), "price", t0, "t"),
              Quote(80.0, "price", t0, "t"),                     # jump: rejected
              Quote(98.6, "price", t0 - timedelta(minutes=5), "t"),
              RuntimeError("feed down")]

    def fetch(_):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    mon = LiveMonitor(Bond(), Position(), feed, fetch=fetch, clock=lambda: clock["now"])
    statuses = []
    logging.disable(logging.CRITICAL)
    for dt in (0, 10, 20, 30, 40, 400):
        clock["now"] = t0 + timedelta(seconds=dt)
        statuses.append(mon.step()[0])
    logging.disable(logging.NOTSET)
    expected = ["OK", "REUSED", "REUSED", "REUSED", "STALE", "NO DATA"]
    check("Live loop: missing/NaN/jump/stale/error", statuses == expected, " ".join(statuses))

    w = max(len(r[0]) for r in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{w}}  {detail}")
    print(f"\nroot finder fallback: {'scipy brentq' if brentq else 'bisection (scipy missing)'}")
    return all(ok for _, ok, _ in results)


# ---------------------------------------------------------------------------
# 8. Command line
# ---------------------------------------------------------------------------
def load_config(path: Optional[str]) -> Tuple[Bond, Position, FeedConfig]:
    """JSON file with optional "bond", "position" and "feed" objects; dates as ISO strings.
    e.g. {"bond": {"coupon_rate": 0.0425, "maturity": "2034-11-15", "day_count": "ACT/ACT"},
          "position": {"purchase_settle": "2025-03-03", "purchase_clean": 98.25},
          "feed": {"source": "bloomberg", "ticker": "US91282CLW90 Govt", "interval_s": 15}}"""
    cfg = {}
    if path:
        with open(path) as fh:
            cfg = json.load(fh)

    def build(cls, raw):
        kw = {}
        for f in fields(cls):
            if f.name in raw:
                v = raw[f.name]
                if isinstance(getattr(cls(), f.name), date) or f.name == "settle_date":
                    v = date.fromisoformat(v) if v else None
                kw[f.name] = v
        return cls(**kw)

    return (build(Bond, cfg.get("bond", {})), build(Position, cfg.get("position", {})),
            build(FeedConfig, cfg.get("feed", {})))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="JSON file with bond / position / feed settings")
    ap.add_argument("--validate", action="store_true", help="run validation and edge-case tests")
    ap.add_argument("--example", action="store_true", help="print the worked example")
    ap.add_argument("--source", choices=("mock", "fedinvest", "bloomberg", "refinitiv"))
    ap.add_argument("--ticker")
    ap.add_argument("--kind", choices=("price", "yield"))
    ap.add_argument("--interval", type=float, help="polling interval, seconds")
    ap.add_argument("--iterations", type=int, help="stop after N updates (default: forever)")
    ap.add_argument("--logfile", help="also append the log to this file")
    args = ap.parse_args()

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.logfile:
        handlers.append(logging.FileHandler(args.logfile))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    if args.validate:
        return 0 if run_validation() else 1
    if args.example:
        worked_example()
        return 0

    bond, pos, feed = load_config(args.config)
    overrides = {k: v for k, v in (("source", args.source), ("ticker", args.ticker),
                                   ("kind", args.kind), ("interval_s", args.interval))
                 if v is not None}
    feed = replace(feed, **overrides)
    if feed.source == "fedinvest":
        # Treasury terms come from the price file unless a config file gives the bond.
        row, _ = treasury_row(feed)
        if row is None:
            log.error("CUSIP %r is not in %s", feed.ticker, feed.data_url)
            return 1
        has_bond = False
        if args.config:
            with open(args.config) as fh:
                has_bond = "bond" in json.load(fh)
        if not has_bond:
            bond = replace(bond, coupon_rate=row["rate"], frequency=2, day_count="ACT/ACT",
                           maturity=date.fromisoformat(row["maturity"]))
        # End-of-day data: a quote is hours old by design (4 days covers a long weekend).
        feed = replace(feed, stale_after_s=max(feed.stale_after_s, 4 * 86400.0),
                       max_reuse_s=max(feed.max_reuse_s, 4 * 86400.0))
    log.info("bond %.3f%% %s f=%d %s | bought %s @ %.4f | feed %s %s every %.1fs",
             100 * bond.coupon_rate, bond.maturity, bond.frequency, bond.day_count,
             pos.purchase_settle, pos.purchase_clean, feed.source, feed.ticker, feed.interval_s)
    LiveMonitor(bond, pos, feed).run(args.iterations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
