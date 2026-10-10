// Same checks as `python python/bond_returns.py --validate`, plus a grid of cases
// computed by the Python reference (test/golden.json, from python/make_golden.py).
import { describe, expect, it } from 'vitest';
import {
  type NewtonStep, addMonths, brent, cleanPrice, computeMetrics, couponDates, couponsReceived,
  DAY_COUNTS, days30360, fmtISO, makeBond, parseISO, previousBusinessDay, settleState, solveYtm,
} from '../src/bond';
import { isStale } from '../src/feed';
import golden from './golden.json';

const d = parseISO;

describe('validation against published values', () => {
  it('Excel YIELD example -> 6.5%', () => {
    const b = makeBond({ couponRate: 0.0575, maturity: d('2016-11-15'), dayCount: '30/360' });
    const { ytm } = solveYtm(b, settleState(b, d('2008-02-15')), 95.04287);
    expect(Math.abs(ytm - 0.065)).toBeLessThan(1e-6);
  });

  it('Excel PRICE example -> 94.63436', () => {
    const b = makeBond({ couponRate: 0.0575, maturity: d('2017-11-15'), dayCount: '30/360' });
    expect(cleanPrice(b, settleState(b, d('2008-02-15')), 0.065)).toBeCloseTo(94.63436, 5);
  });

  it('worked example (docs/METHOD.md section 8)', () => {
    const b = makeBond({ maturity: d('2029-06-15') });
    const trace: NewtonStep[] = [];
    const st = settleState(b, d('2026-10-12'));
    expect([st.A, st.E, st.DSC, st.accrued]).toEqual([117, 180, 63, 1.95]);
    solveYtm(b, st, 98.5, null, 1e-11, trace);
    expect(trace).toHaveLength(3);
    const m = computeMetrics(b, { purchaseSettle: d('2025-09-15'), purchaseClean: 97 }, d('2026-10-12'), 98.5);
    expect(m.ytm).toBeCloseTo(0.0661643, 7);
    expect(m.currentYield).toBeCloseTo(0.0609137, 7);
    expect(m.hpr).toBeCloseTo(7.95 / 98.5, 12);
    expect(m.hprAnnualised).toBeCloseTo(0.0749484, 7);
    expect(m.holdingDays).toBe(392);
    expect(m.couponsReceived).toBe(6);
  });
});

describe('edge cases', () => {
  it('round trip price -> yield -> price for every day count and frequency', () => {
    let worst = 0;
    for (const dayCount of DAY_COUNTS) {
      for (const frequency of [1, 2, 4, 12] as const) {
        const b = makeBond({ couponRate: 0.045, frequency, maturity: d('2034-03-31'), dayCount });
        for (const settle of ['2026-10-12', '2033-12-01']) {
          const st = settleState(b, d(settle));
          for (const y of [-0.02, -0.005, 0, 0.03, 0.12, 0.4]) {
            worst = Math.max(worst, Math.abs(solveYtm(b, st, cleanPrice(b, st, y)).ytm - y));
          }
        }
      }
    }
    expect(worst).toBeLessThan(1e-10);
  });

  it('zero-coupon bond matches the closed form', () => {
    const b = makeBond({ couponRate: 0, maturity: d('2031-05-15'), dayCount: 'ACT/ACT' });
    const st = settleState(b, d('2026-11-15'));
    const p = 100 / (1 + 0.04 / 2) ** 9;
    expect(solveYtm(b, st, p).ytm).toBeCloseTo(0.04, 12);
    expect(st.accrued).toBe(0);
    expect(computeMetrics(b, null, st.settle, p).currentYield).toBe(0);
  });

  it('settlement on a coupon date: no accrued, full period, coupon to the seller', () => {
    const b = makeBond({ maturity: d('2029-06-15') });
    const st = settleState(b, d('2026-12-15'));
    expect(st.accrued).toBe(0);
    expect(st.w).toBe(1);
    expect(fmtISO(st.flows[0].payDate)).toBe('2027-06-15');
    expect(couponsReceived(b, d('2025-12-15'), d('2026-06-15'))).toBe(3);
  });

  it('negative yield when price exceeds the remaining cash flows', () => {
    const b = makeBond({ couponRate: 0.005, frequency: 1, maturity: d('2028-10-12'), dayCount: 'ACT/ACT' });
    const st = settleState(b, d('2026-10-12'));
    const { ytm } = solveYtm(b, st, 102);
    expect(ytm).toBeLessThan(0);
    expect(cleanPrice(b, st, ytm)).toBeCloseTo(102, 9);
  });

  it('last period with simple interest allows yields below -f', () => {
    const b = makeBond({ couponRate: 0, frequency: 1, maturity: d('2027-01-31'), dayCount: 'ACT/ACT' });
    const st = settleState(b, d('2026-12-15'));
    const { ytm } = solveYtm(b, st, 131);
    expect(ytm).toBeLessThan(-1);
    expect(cleanPrice(b, st, ytm)).toBeCloseTo(131, 9);
  });

  it('Newton from a hopeless start falls back to Brent', () => {
    const b = makeBond({ maturity: d('2056-06-15') });
    const st = settleState(b, d('2026-10-12'));
    const r = solveYtm(b, st, 98.5, 50);
    expect(r.method).toBe('brent');
    expect(cleanPrice(b, st, r.ytm)).toBeCloseTo(98.5, 9);
  });

  it('Brent finds a simple root', () => {
    expect(brent((x) => x * x - 2, 0, 2)[0]).toBeCloseTo(Math.SQRT2, 14);
  });

  it('month-end schedule does not drift', () => {
    const [prev, fut] = couponDates(makeBond({ maturity: d('2030-08-31') }), d('2026-10-12'));
    expect([fmtISO(prev), fmtISO(fut[0])]).toEqual(['2026-08-31', '2027-02-28']);
    expect(fmtISO(addMonths(d('2024-01-31'), 1))).toBe('2024-02-29');
    expect(days30360(d('2026-01-31'), d('2026-03-31'))).toBe(60);
  });

  it('matured bond and bad inputs are refused', () => {
    const b = makeBond({ maturity: d('2029-06-15') });
    expect(() => settleState(b, d('2029-06-15'))).toThrow(/matured/);
    expect(() => solveYtm(b, settleState(b, d('2026-10-12')), 0)).toThrow();
    expect(() => parseISO('2026-02-30')).toThrow();
  });

  it('end-of-day staleness uses business days', () => {
    // Monday 12 Oct 2026: Friday's prices are current, Thursday's are stale
    expect(fmtISO(previousBusinessDay(d('2026-10-12')))).toBe('2026-10-09');
    expect(isStale(d('2026-10-09'), d('2026-10-12'))).toBe(false);
    expect(isStale(d('2026-10-08'), d('2026-10-12'))).toBe(true);
  });
});

describe('matches the Python reference (golden.json)', () => {
  type Case = (typeof golden)[number];
  it.each(golden.map((c, i) => [i, c] as [number, Case]))('case %i', (_i, c) => {
    const b = makeBond({ ...c.bond, maturity: d(c.bond.maturity), frequency: c.bond.frequency as 1 | 2 | 4 | 12,
      dayCount: c.bond.dayCount as (typeof DAY_COUNTS)[number] });
    const st = settleState(b, d(c.settle));
    const m = computeMetrics(b, { purchaseSettle: d(c.purchaseSettle), purchaseClean: c.purchaseClean }, st.settle, c.clean);
    const close = (a: number | null, e: number | null) =>
      e === null ? expect(a).toBeNull() : expect(Math.abs((a as number) - e)).toBeLessThan(1e-9);
    expect(st.flows.length).toBe(c.nFlows);
    close(st.accrued, c.accrued);
    close(st.w, c.w);
    close(m.ytm, c.ytm);
    close(m.currentYield, c.currentYield);
    close(m.couponsReceived, c.couponsReceived);
    close(m.hpr, c.hpr);
    close(m.hprAnnualised, c.hprAnnualised);
    close(cleanPrice(b, st, 0.05), c.priceAt5pct);
  });
});
