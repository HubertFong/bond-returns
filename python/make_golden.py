#!/usr/bin/env python3
"""Write test/golden.json: a grid of cases computed by bond_returns.py, which the
TypeScript tests (test/bond.test.ts) must reproduce to 1e-9.
Run from the repo root: python python/make_golden.py"""
import itertools
import json
import pathlib
import sys
from dataclasses import replace
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bond_returns import Bond, Position, clean_price, compute_metrics, settle_state  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / "test" / "golden.json"

MATURITIES = [date(2029, 6, 15), date(2030, 8, 31), date(2034, 2, 28), date(2027, 1, 31), date(2056, 11, 15)]
SETTLES = [date(2026, 10, 12), date(2026, 12, 15), date(2026, 8, 31), date(2026, 3, 2)]
CLEANS = [62.5, 98.5, 100.0, 103.75, 131.0]


def bond_dict(b: Bond) -> dict:
    return {"couponRate": b.coupon_rate, "frequency": b.frequency, "maturity": b.maturity.isoformat(),
            "dayCount": b.day_count, "redemption": b.redemption, "simpleLastPeriod": b.simple_last_period}


def main() -> None:
    cases = []
    for i, (dc, f, mat, settle, coupon) in enumerate(itertools.product(
            ("30/360", "ACT/ACT", "ACT/365"), (1, 2, 4, 12), MATURITIES, SETTLES, (0.0, 0.0425))):
        b = Bond(coupon_rate=coupon, frequency=f, maturity=mat, day_count=dc)
        if settle >= mat:
            continue
        st = settle_state(b, settle)
        for clean in (98.5, CLEANS[i % len(CLEANS)]):  # par-ish plus a rotating extreme
            pos = Position(date(2025, 9, 15), 97.0)
            m = compute_metrics(b, pos, settle, clean)
            cases.append({"bond": bond_dict(b), "settle": settle.isoformat(), "clean": clean,
                          "purchaseSettle": pos.purchase_settle.isoformat(), "purchaseClean": pos.purchase_clean,
                          "accrued": st.accrued, "w": st.w, "nFlows": len(st.flows), "ytm": m.ytm,
                          "currentYield": m.current_yield, "couponsReceived": m.coupons_received,
                          "hpr": m.hpr, "hprAnnualised": m.hpr_annualised,
                          "priceAt5pct": clean_price(b, st, 0.05)})
    # one cash flow left, both conventions
    for simple in (True, False):
        b = replace(Bond(coupon_rate=0.05, maturity=date(2027, 3, 15)), simple_last_period=simple)
        st = settle_state(b, date(2026, 10, 12))
        m = compute_metrics(b, Position(date(2026, 9, 15), 99.0), st.settle, 100.5)
        cases.append({"bond": bond_dict(b), "settle": "2026-10-12", "clean": 100.5,
                      "purchaseSettle": "2026-09-15", "purchaseClean": 99.0,
                      "accrued": st.accrued, "w": st.w, "nFlows": len(st.flows), "ytm": m.ytm,
                      "currentYield": m.current_yield, "couponsReceived": m.coupons_received,
                      "hpr": m.hpr, "hprAnnualised": m.hpr_annualised, "priceAt5pct": clean_price(b, st, 0.05)})
    OUT.write_text(json.dumps(cases, separators=(",", ":")))
    print(f"wrote {len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
