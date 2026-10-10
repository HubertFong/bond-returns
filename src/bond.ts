// Bond maths: a one-to-one port of python/bond_returns.py (sections 2-4).
//
// Conventions (see docs/METHOD.md):
//   * Prices per 100 face; yields as decimals (0.065 = 6.5%).
//   * Street-convention YTM, compounded at the coupon frequency, first period
//     discounted by w = DSC / E; simple interest when one cash flow is left.
//   * Regular schedule rolled back from maturity, no odd coupons, no ex-coupon period.
//   * Dates are integer day numbers (days since 1970-01-01, UTC) so no time zone
//     can move a date.

export type Day = number;
export type DayCount = '30/360' | 'ACT/ACT' | 'ACT/365';
export const DAY_COUNTS: DayCount[] = ['30/360', 'ACT/ACT', 'ACT/365'];

const MS_PER_DAY = 86_400_000;

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------
export const toDay = (y: number, m: number, d: number): Day => Date.UTC(y, m - 1, d) / MS_PER_DAY;

export function ymd(day: Day): [number, number, number] {
  const dt = new Date(day * MS_PER_DAY);
  return [dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate()];
}

export function parseISO(s: string): Day {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s.trim());
  if (!m) throw new Error(`bad date ${JSON.stringify(s)}, expected YYYY-MM-DD`);
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const day = toDay(y, mo, d);
  const [y2, mo2, d2] = ymd(day);
  if (y2 !== y || mo2 !== mo || d2 !== d) throw new Error(`no such date ${s}`);
  return day;
}

export const fmtISO = (day: Day): string => new Date(day * MS_PER_DAY).toISOString().slice(0, 10);

const daysInMonth = (y: number, m: number): number => new Date(Date.UTC(y, m, 0)).getUTCDate();

const isMonthEnd = (day: Day): boolean => {
  const [y, m, d] = ymd(day);
  return d === daysInMonth(y, m);
};

/** Shift by whole months, clipping to the month's last day (or forcing it). */
export function addMonths(day: Day, months: number, monthEnd = false): Day {
  const [y0, m0, d0] = ymd(day);
  const total = m0 - 1 + months;
  const y = y0 + Math.floor(total / 12);
  const m = (((total % 12) + 12) % 12) + 1;
  const last = daysInMonth(y, m);
  return toDay(y, m, monthEnd ? last : Math.min(d0, last));
}

/** Weekends only; plug a holiday calendar in here for production use. */
export function addBusinessDays(day: Day, n: number): Day {
  let d = day;
  while (n > 0) {
    d += 1;
    const wd = new Date(d * MS_PER_DAY).getUTCDay(); // 0 = Sunday
    if (wd !== 0 && wd !== 6) n -= 1;
  }
  return d;
}

/** The business day before `day` (weekends only). */
export function previousBusinessDay(day: Day): Day {
  let d = day - 1;
  while ([0, 6].includes(new Date(d * MS_PER_DAY).getUTCDay())) d -= 1;
  return d;
}

// ---------------------------------------------------------------------------
// Bond, schedule and day counts
// ---------------------------------------------------------------------------
export interface Bond {
  faceValue: number;        // currency amount per bond; prices are per 100
  couponRate: number;       // annual, decimal; 0 for a zero-coupon bond
  frequency: 1 | 2 | 4 | 12;
  maturity: Day;
  dayCount: DayCount;
  redemption: number;       // per 100 face
  eomRule: boolean;         // month-end maturity -> every coupon on a month-end
  simpleLastPeriod: boolean; // Excel/SIA: simple interest when one cash flow is left
}

export interface Position {
  purchaseSettle: Day;
  purchaseClean: number;
}

export function makeBond(p: Partial<Bond> & Pick<Bond, 'maturity'>): Bond {
  const b: Bond = {
    faceValue: 1000, couponRate: 0.06, frequency: 2, dayCount: '30/360',
    redemption: 100, eomRule: true, simpleLastPeriod: true, ...p,
  };
  if (![1, 2, 4, 12].includes(b.frequency)) throw new Error('frequency must be 1, 2, 4 or 12');
  if (!DAY_COUNTS.includes(b.dayCount)) throw new Error(`day count must be one of ${DAY_COUNTS.join(', ')}`);
  if (!(b.couponRate >= 0) || !(b.faceValue > 0) || !(b.redemption > 0)) {
    throw new Error('coupon rate must be >= 0; face value and redemption > 0');
  }
  return b;
}

export const couponPerPeriod = (b: Bond): number => (100 * b.couponRate) / b.frequency;
const monthEndBond = (b: Bond): boolean => b.eomRule && isMonthEnd(b.maturity);

/**
 * [last coupon date <= after, coupon dates > after ... maturity].
 * Each date is rolled from maturity itself so a 31st does not drift to the 30th.
 * If `after` is a coupon date, that coupon belongs to the seller.
 */
export function couponDates(b: Bond, after: Day): [Day, Day[]] {
  if (after >= b.maturity) {
    throw new Error(`bond matured on ${fmtISO(b.maturity)}; settlement ${fmtISO(after)}`);
  }
  const step = 12 / b.frequency;
  const me = monthEndBond(b);
  const future: Day[] = [];
  for (let k = 0; ; k++) {
    const d = addMonths(b.maturity, -k * step, me);
    if (d <= after) return [d, future.reverse()];
    future.push(d);
  }
}

/** 30/360 US (SIA / Excel basis 0). February rules only for month-end bonds. */
export function days30360(d1: Day, d2: Day, monthEnd = false): number {
  const [y1, m1, a] = ymd(d1);
  const [y2, m2, b] = ymd(d2);
  let dd1 = a, dd2 = b;
  const febEnd = (d: Day) => ymd(d)[1] === 2 && isMonthEnd(d);
  if (monthEnd && febEnd(d1) && febEnd(d2)) dd2 = 30;
  if (monthEnd && febEnd(d1)) dd1 = 30;
  if (dd2 === 31 && dd1 >= 30) dd2 = 30;
  if (dd1 === 31) dd1 = 30;
  return 360 * (y2 - y1) + 30 * (m2 - m1) + (dd2 - dd1);
}

export interface Cashflow {
  payDate: Day;
  coupon: number;    // per 100 face
  principal: number; // per 100 face
  amount: number;
}

export interface SettleState {
  settle: Day;
  prevCoupon: Day;
  nextCoupon: Day;
  flows: Cashflow[];
  A: number;       // accrued days: last coupon -> settlement
  E: number;       // days in the coupon period
  DSC: number;     // days: settlement -> next coupon
  w: number;       // DSC / E
  accrued: number; // per 100
}

/** Cash-flow schedule, day-count terms and accrued interest at `settle`. */
export function settleState(b: Bond, settle: Day): SettleState {
  const [prev, future] = couponDates(b, settle);
  const next = future[0];
  const f = b.frequency;
  let A: number, E: number, DSC: number;
  if (b.dayCount === '30/360') {
    E = 360 / f;
    A = days30360(prev, settle, monthEndBond(b));
    DSC = E - A;
  } else if (b.dayCount === 'ACT/ACT') { // ICMA: actual days in the actual period
    E = next - prev;
    A = settle - prev;
    DSC = next - settle;
  } else {                               // ACT/365 Fixed
    E = 365 / f;
    A = settle - prev;
    DSC = next - settle;
  }
  const c = couponPerPeriod(b);
  const flows = future.map((d) => {
    const principal = d === b.maturity ? b.redemption : 0;
    return { payDate: d, coupon: c, principal, amount: c + principal };
  });
  return { settle, prevCoupon: prev, nextCoupon: next, flows, A, E, DSC, w: DSC / E, accrued: (c * A) / E };
}

/** Coupons (per 100) paid in (start, end]. */
export function couponsReceived(b: Bond, start: Day, end: Day): number {
  if (end <= start) return 0;
  const step = 12 / b.frequency;
  const me = monthEndBond(b);
  let total = 0;
  for (let k = 0; ; k++) {
    const d = addMonths(b.maturity, -k * step, me);
    if (d <= start) return total;
    if (d <= end) total += couponPerPeriod(b);
  }
}

// ---------------------------------------------------------------------------
// Pricing and the YTM solver
// ---------------------------------------------------------------------------
/** Dirty price per 100 at yield y and dP/dy:  P = sum_k CF_k / (1 + y/f)^(w + k - 1). */
export function dirtyPrice(b: Bond, st: SettleState, y: number): [number, number] {
  const f = b.frequency, w = st.w;
  if (b.simpleLastPeriod && st.flows.length === 1) {
    const cf = st.flows[0].amount;
    const denom = 1 + (w * y) / f;
    if (denom <= 0) throw new RangeError('yield below the -f/w limit');
    return [cf / denom, (-cf * (w / f)) / denom ** 2];
  }
  const base = 1 + y / f;
  if (base <= 0) throw new RangeError('yield at or below -frequency');
  let pv = 0, dpv = 0;
  st.flows.forEach((cf, k) => {
    const t = w + k; // exponent in coupon periods
    const df = base ** -t;
    pv += cf.amount * df;
    dpv -= (cf.amount * (t / f) * df) / base;
  });
  return [pv, dpv];
}

export const cleanPrice = (b: Bond, st: SettleState, y: number): number => dirtyPrice(b, st, y)[0] - st.accrued;

/** Start value: (annual coupon + pull to par per year) / average price. */
export function approxYtm(b: Bond, st: SettleState, clean: number): number {
  const years = Math.max((b.maturity - st.settle) / 365.25, 1 / 365.25);
  const y = (100 * b.couponRate + (b.redemption - clean) / years) / ((b.redemption + clean) / 2);
  return Math.min(Math.max(y, -0.5 * b.frequency), 1.0);
}

/** Brent's method (zeroin). Needs g(a), g(b) of opposite sign. Returns [root, iterations]. */
export function brent(g: (x: number) => number, a: number, b: number, xtol = 1e-15, maxIter = 500): [number, number] {
  const EPS = Number.EPSILON;
  let fa = g(a), fb = g(b);
  if (fa * fb > 0) throw new Error('root not bracketed');
  let c = b, fc = fb, d = b - a, e = d;
  for (let i = 1; i <= maxIter; i++) {
    if ((fb > 0 && fc > 0) || (fb < 0 && fc < 0)) { c = a; fc = fa; d = b - a; e = d; }
    if (Math.abs(fc) < Math.abs(fb)) { a = b; b = c; c = a; fa = fb; fb = fc; fc = fa; }
    const tol1 = 2 * EPS * Math.abs(b) + 0.5 * xtol;
    const xm = 0.5 * (c - b);
    if (Math.abs(xm) <= tol1 || fb === 0) return [b, i];
    if (Math.abs(e) >= tol1 && Math.abs(fa) > Math.abs(fb)) {
      const s = fb / fa;
      let p: number, q: number;
      if (a === c) {                     // secant
        p = 2 * xm * s; q = 1 - s;
      } else {                           // inverse quadratic interpolation
        const qq = fa / fc, r = fb / fc;
        p = s * (2 * xm * qq * (qq - r) - (b - a) * (r - 1));
        q = (qq - 1) * (r - 1) * (s - 1);
      }
      if (p > 0) q = -q;
      p = Math.abs(p);
      if (2 * p < Math.min(3 * xm * q - Math.abs(tol1 * q), Math.abs(e * q))) { e = d; d = p / q; }
      else { d = xm; e = d; }             // interpolation failed: bisect
    } else { d = xm; e = d; }
    a = b; fa = fb;
    b += Math.abs(d) > tol1 ? d : (xm >= 0 ? tol1 : -tol1);
    fb = g(b);
  }
  return [b, maxIter];
}

export interface YtmResult { ytm: number; method: 'newton' | 'brent'; iterations: number }
export interface NewtonStep { i: number; y: number; price: number; slope: number; diff: number }

/**
 * Solve P_dirty(y) = clean + accrued. Newton-Raphson first; if it diverges, leaves
 * the domain y > -f or stalls, bracket the root and use Brent's method. Price is
 * strictly decreasing in y, so every positive price has exactly one yield.
 */
export function solveYtm(b: Bond, st: SettleState, clean: number, guess?: number | null,
  tol = 1e-11, trace?: NewtonStep[]): YtmResult {
  if (!(Number.isFinite(clean) && clean > 0)) throw new Error(`invalid clean price ${clean}`);
  const target = clean + st.accrued;
  // Domain: 1 + y/f > 0, or 1 + w*y/f > 0 when the last period uses simple interest.
  const simple = b.simpleLastPeriod && st.flows.length === 1;
  const floor = (simple && st.w > 0 ? -b.frequency / st.w : -b.frequency) + 1e-9;
  const g = (y: number) => dirtyPrice(b, st, y)[0] - target;

  let y = guess != null && Number.isFinite(guess) && guess > floor ? guess : approxYtm(b, st, clean);
  for (let i = 1; i <= 50; i++) {
    const [p, dp] = dirtyPrice(b, st, y);
    const diff = p - target;
    trace?.push({ i, y, price: p, slope: dp, diff });
    if (Math.abs(diff) < tol) return { ytm: y, method: 'newton', iterations: i };
    if (!(Number.isFinite(dp) && dp < 0)) break;
    const yNext = y - diff / dp;
    if (!Number.isFinite(yNext) || yNext <= floor) break; // would leave the domain
    y = yNext;
  }

  // Fallback: bracket the root, then Brent.
  let lo = -0.01, hi = 0.1;
  for (let k = 0; k < 200; k++) {                    // g(lo) > 0: walk towards the floor
    if (lo > floor && g(lo) > 0) break;
    lo = floor + (Math.max(lo, floor) - floor) / 2;
  }
  for (let k = 0; k < 100; k++) {                    // g(hi) < 0: widen upwards
    if (g(hi) < 0) break;
    hi = 2 * hi + 0.1;
  }
  if (!(g(lo) > 0 && g(hi) < 0)) throw new Error(`could not bracket a yield for clean price ${clean}`);
  const [root, iterations] = brent(g, lo, hi);
  return { ytm: root, method: 'brent', iterations };
}

// ---------------------------------------------------------------------------
// Return metrics
// ---------------------------------------------------------------------------
export interface Metrics {
  settle: Day;
  clean: number;
  accrued: number;
  dirty: number;
  marketValue: number;           // dirty value of one bond, in currency
  currentYield: number | null;
  ytm: number;
  solver: string;
  iterations: number;
  couponsReceived: number;       // per 100, since purchase
  holdingDays: number | null;
  hpr: number | null;            // not annualised
  hprAnnualised: number | null;  // compound, ACT/365
  state: SettleState;
}

export function computeMetrics(b: Bond, pos: Position | null, settle: Day, clean: number,
  ytmGuess?: number | null): Metrics {
  const st = settleState(b, settle);
  const dirty = clean + st.accrued;
  const { ytm, method, iterations } = solveYtm(b, st, clean, ytmGuess);
  const currentYield = clean > 0 ? (100 * b.couponRate) / clean : null;

  // HPR = (P_dirty,now + coupons received - P_dirty,paid) / P_dirty,paid
  let hpr: number | null = null, ann: number | null = null, days: number | null = null, cpn = 0;
  if (pos && Number.isFinite(pos.purchaseClean) && pos.purchaseClean > 0) {
    days = settle - pos.purchaseSettle;
    if (days >= 0) {
      cpn = couponsReceived(b, pos.purchaseSettle, settle);
      const paid = pos.purchaseClean + settleState(b, pos.purchaseSettle).accrued;
      hpr = (dirty + cpn - paid) / paid;
      if (days > 0 && hpr > -1) ann = (1 + hpr) ** (365 / days) - 1;
    }
  }
  return {
    settle, clean, accrued: st.accrued, dirty, marketValue: (dirty / 100) * b.faceValue,
    currentYield, ytm, solver: method, iterations, couponsReceived: cpn,
    holdingDays: days, hpr, hprAnnualised: ann, state: st,
  };
}
