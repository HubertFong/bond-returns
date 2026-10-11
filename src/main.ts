/**
 * main.ts - the page: reads the form, gets a price, recomputes, renders, repeats.
 *
 *   form inputs -> buildInputs() -> getLivePrice() -> computeMetrics() -> render
 *        ^                                                                  |
 *        +---------------- every N seconds, or on any form edit -----------+
 *
 * Sections:
 *   Form state      read, write and remember (localStorage) the inputs
 *   Treasury list   the CUSIP picker, filled from the price file
 *   Rendering       the result tiles, details table, cash-flow table and log
 *   The loop        tick(): one fetch -> check -> recompute -> render cycle
 *   Start-up        init(): load prices, restore inputs, wire events, start the loop
 *
 * All finance lives in bond.ts and all data access in feed.ts; this file only
 * connects them to the HTML in index.html.
 */

import './style.css';
import {
  type Bond, type Day, type DayCount, type Metrics, type Position,
  addBusinessDays, computeMetrics, fmtISO, makeBond, parseISO, toDay,
} from './bond';
import {
  type FeedConfig, type PriceFile, type Quote, type Status, type TreasuryRow,
  cachedPriceFile, getLivePrice, isStale, loadPriceFile, quoteProblem,
} from './feed';

// ---------------------------------------------------------------------------
// Form state
// ---------------------------------------------------------------------------
/** The form's raw values (strings, as typed). Validated in buildInputs(). */
interface FormState {
  source: 'fedinvest' | 'manual';
  cusip: string;
  manualPrice: string;
  faceValue: string;
  couponPct: string;
  frequency: string;
  maturity: string;
  dayCount: string;
  settleLag: string;
  settleDate: string;
  purchaseSettle: string;
  purchaseClean: string;
  intervalS: string;
}

const STORAGE_KEY = 'bond-returns:v1';
const FIELDS: (keyof FormState)[] = [
  'cusip', 'manualPrice', 'faceValue', 'couponPct', 'frequency', 'maturity', 'dayCount',
  'settleLag', 'settleDate', 'purchaseSettle', 'purchaseClean', 'intervalS',
];
const BOND_FIELDS: (keyof FormState)[] = ['couponPct', 'frequency', 'maturity', 'dayCount'];

/** The example in docs/METHOD.md section 8: expect YTM 6.6164% and HPR 8.0711%. */
const WORKED_EXAMPLE: FormState = {
  source: 'manual', cusip: '', manualPrice: '98.50', faceValue: '1000', couponPct: '6',
  frequency: '2', maturity: '2029-06-15', dayCount: '30/360', settleLag: '1',
  settleDate: '2026-10-12', purchaseSettle: '2025-09-15', purchaseClean: '97.00', intervalS: '60',
};

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
};
const input = (name: keyof FormState) => $<HTMLInputElement | HTMLSelectElement>(name);

function readForm(): FormState {
  const source = ($<HTMLInputElement>('srcManual').checked ? 'manual' : 'fedinvest') as FormState['source'];
  const s = { source } as FormState;
  for (const k of FIELDS) (s[k] as string) = input(k).value.trim();
  return s;
}

function writeForm(s: FormState): void {
  $<HTMLInputElement>(s.source === 'manual' ? 'srcManual' : 'srcFedinvest').checked = true;
  for (const k of FIELDS) input(k).value = s[k] ?? '';
  syncSourceUi();
}

function saveState(s: FormState): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch { /* storage unavailable */ }
}

function loadState(): FormState | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? { ...WORKED_EXAMPLE, ...JSON.parse(raw) } : null;
  } catch {
    return null;
  }
}

/** Show the CUSIP or the manual-price field, and lock the bond terms in Treasury mode
 *  (they come from the price file there). */
function syncSourceUi(): void {
  const manual = $<HTMLInputElement>('srcManual').checked;
  $('cusipRow').hidden = manual;
  $('manualRow').hidden = !manual;
  for (const k of BOND_FIELDS) input(k).disabled = !manual; // Treasury terms come from the file
}

const today = (): Day => {
  const n = new Date();
  return toDay(n.getFullYear(), n.getMonth() + 1, n.getDate());
};

function num(s: string, what: string): number {
  const v = Number(s);
  if (s === '' || !Number.isFinite(v)) throw new Error(`${what}: enter a number`);
  return v;
}

/** Turn the form into typed inputs. Throws a readable message on the first bad field. */
function buildInputs(s: FormState): { bond: Bond; pos: Position | null; settle: Day; feed: FeedConfig } {
  const bond = makeBond({
    faceValue: num(s.faceValue, 'Face value'),
    couponRate: num(s.couponPct, 'Coupon') / 100,
    frequency: num(s.frequency, 'Frequency') as Bond['frequency'],
    maturity: parseISO(s.maturity),
    dayCount: s.dayCount as DayCount,
  });
  const settle = s.settleDate ? parseISO(s.settleDate) : addBusinessDays(today(), num(s.settleLag, 'Settlement lag'));
  const pos = s.purchaseSettle && s.purchaseClean
    ? { purchaseSettle: parseISO(s.purchaseSettle), purchaseClean: num(s.purchaseClean, 'Purchase price') }
    : null;
  const feed: FeedConfig = s.source === 'manual'
    ? { source: 'manual', price: num(s.manualPrice, 'Clean price'), today: today() }
    : { source: 'fedinvest', cusip: s.cusip.toUpperCase() };
  return { bond, pos, settle, feed };
}

// ---------------------------------------------------------------------------
// Treasury list
// ---------------------------------------------------------------------------
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const niceDate = (iso: string) => {
  const [y, m, d] = iso.split('-').map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
};
const describe = (r: TreasuryRow) =>
  `${(100 * r.rate).toFixed(3)}% ${r.type.includes('BOND') ? 'bond' : 'note'} due ${niceDate(r.maturity)}`;

function fillDatalist(file: PriceFile): void {
  const list = $('cusips');
  list.replaceChildren(...[...file.securities]
    .sort((a, b) => a.maturity.localeCompare(b.maturity))
    .map((r) => Object.assign(document.createElement('option'), { value: r.cusip, label: describe(r) })));
}

/** Fill coupon, maturity, frequency and day count from the price file for a CUSIP.
 *  Treasuries pay semi-annually and accrue ACT/ACT. Returns false if the CUSIP is unknown. */
function applyTreasury(cusip: string): boolean {
  const row = cachedPriceFile()?.securities.find((r) => r.cusip === cusip.toUpperCase());
  $('cusipInfo').textContent = row ? describe(row) : cusip ? 'Not in the latest FedInvest file' : '';
  if (!row) return false;
  input('couponPct').value = String(+(100 * row.rate).toFixed(6));
  input('maturity').value = row.maturity;
  input('frequency').value = '2';
  input('dayCount').value = 'ACT/ACT';
  return true;
}

/** Default CUSIP: the note maturing closest to ten years from today. */
function defaultCusip(file: PriceFile): string {
  const target = today() + 3652;
  let best = file.securities[0];
  for (const r of file.securities) {
    if (Math.abs(parseISO(r.maturity) - target) < Math.abs(parseISO(best.maturity) - target)) best = r;
  }
  return best?.cusip ?? '';
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
const pct = (x: number | null, dp = 4) => (x == null ? '—' : `${(100 * x).toFixed(dp)}%`);
const px = (x: number) => x.toFixed(4);
const money = (x: number) => x.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function setBadge(status: Status | 'LOADING' | 'ERROR', note: string): void {
  const b = $('badge');
  b.textContent = status;
  b.dataset.status = status.split('+')[0];
  $('asof').textContent = note;
}

function renderMetrics(m: Metrics, bond: Bond): void {
  $('kYtm').textContent = pct(m.ytm);
  $('kCy').textContent = pct(m.currentYield);
  $('kHpr').textContent = pct(m.hpr);
  $('kAnn').textContent = pct(m.hprAnnualised);
  $('kAnnNote').textContent = m.holdingDays != null && m.holdingDays < 30 && m.hpr != null
    ? 'under 30 days: noisy' : 'compound, ACT/365';
  const st = m.state;
  const rows: [string, string][] = [
    ['Settlement date', fmtISO(m.settle)],
    ['Clean price', px(m.clean)],
    ['Accrued interest', `${px(m.accrued)} (${st.A}/${+st.E.toFixed(2)} days)`],
    ['Last / next coupon', `${fmtISO(st.prevCoupon)} / ${fmtISO(st.nextCoupon)}`],
    ['Dirty price', px(m.dirty)],
    [`Market value per ${money(bond.faceValue)} face`, money(m.marketValue)],
    ['Coupons received since purchase', m.hpr == null ? '—' : `${px(m.couponsReceived)} per 100`],
    ['Days held', m.holdingDays == null ? '—' : String(m.holdingDays)],
    ['YTM solver', `${m.solver}, ${m.iterations} iteration${m.iterations === 1 ? '' : 's'}`],
  ];
  $('detail').replaceChildren(...rows.map(([k, v]) => {
    const tr = document.createElement('tr');
    tr.append(Object.assign(document.createElement('th'), { textContent: k, scope: 'row' }),
      Object.assign(document.createElement('td'), { textContent: v }));
    return tr;
  }));

  // Cash-flow schedule with present values at the solved YTM
  const f = bond.frequency, y = m.ytm;
  const simple = bond.simpleLastPeriod && st.flows.length === 1;
  let total = 0;
  const body = st.flows.map((cf, k) => {
    const t = st.w + k;
    const df = simple ? 1 / (1 + (st.w * y) / f) : (1 + y / f) ** -t;
    total += cf.amount * df;
    return [fmtISO(cf.payDate), cf.coupon.toFixed(4), cf.principal ? cf.principal.toFixed(2) : '', cf.amount.toFixed(4),
      t.toFixed(4), df.toFixed(6), (cf.amount * df).toFixed(4)];
  });
  body.push(['Total = dirty price', '', '', '', '', '', total.toFixed(4)]);
  $('flowsBody').replaceChildren(...body.map((cells, i) => {
    const tr = document.createElement('tr');
    if (i === body.length - 1) tr.className = 'total';
    tr.append(...cells.map((c) => Object.assign(document.createElement('td'), { textContent: c })));
    return tr;
  }));
}

function clearMetrics(): void {
  for (const id of ['kYtm', 'kCy', 'kHpr', 'kAnn']) $(id).textContent = '—';
  $('detail').replaceChildren();
  $('flowsBody').replaceChildren();
}

function logLine(text: string, status: string): void {
  const li = document.createElement('li');
  li.dataset.status = status.split('+')[0];
  li.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
  const log = $('log');
  log.prepend(li);
  while (log.children.length > 12) log.lastElementChild?.remove();
}

function showError(msg: string | null): void {
  const box = $('error');
  box.hidden = !msg;
  box.textContent = msg ?? '';
}

// ---------------------------------------------------------------------------
// The loop: fetch -> check -> recompute -> render
// ---------------------------------------------------------------------------
let lastGood: { key: string; quote: Quote } | null = null; // keyed by bond, never reused for another
let lastYtm: number | null = null;                          // warm start for the YTM solver
let timer: number | undefined;                              // the pending refresh
let nextAt = 0;                                             // when it fires, for the countdown

/**
 * One refresh:
 *   1. Read and validate the form (an error is shown, nothing else happens).
 *   2. Ask getLivePrice() for a quote and reject implausible values.
 *   3. If there is no usable quote, reuse the last good one for the same bond (REUSED);
 *      with nothing to reuse, show NO DATA.
 *   4. Flag end-of-day prices older than the previous business day (STALE).
 *   5. Recompute, warm-starting the YTM solver from the last result, and render.
 */
async function tick(): Promise<void> {
  const s = readForm();
  saveState(s);
  let built: ReturnType<typeof buildInputs>;
  try {
    built = buildInputs(s);
  } catch (e) {
    showError((e as Error).message);
    clearMetrics();
    setBadge('ERROR', 'Fix the highlighted input');
    return;
  }
  const { bond, pos, settle, feed } = built;
  const key = feed.source === 'fedinvest' ? `fedinvest:${feed.cusip}` : 'manual';

  let quote: Quote | null = null;
  let status: Status = 'OK';
  let why = '';
  try {
    quote = await getLivePrice(feed);
    const problem = quote && quoteProblem(quote);
    if (problem) { why = problem; quote = null; }
    else if (!quote) why = feed.source === 'manual' ? 'enter a clean price' : `no price for ${feed.source === 'fedinvest' ? feed.cusip || 'blank CUSIP' : ''}`;
  } catch (e) {
    why = (e as Error).message;
  }
  if (quote) {
    lastGood = { key, quote };
  } else if (lastGood?.key === key) {
    quote = lastGood.quote;           // missing or rejected this tick: reuse the last good one
    status = 'REUSED';
  } else {
    showError(`No data: ${why}`);
    clearMetrics();
    setBadge('NO DATA', why);
    logLine(`NO DATA (${why})`, 'NO DATA');
    return;
  }
  if (feed.source === 'fedinvest' && isStale(quote.priceDate, today())) {
    status = status === 'OK' ? 'STALE' : 'REUSED+STALE';
  }

  try {
    const m = computeMetrics(bond, pos, settle, quote.clean, lastYtm);
    lastYtm = m.ytm;
    showError(null);
    renderMetrics(m, bond);
    const file = cachedPriceFile();
    const note = feed.source === 'manual'
      ? 'Manual price'
      : `Prices as of ${niceDate(fmtISO(quote.priceDate))} · fetched ${new Date(quote.fetchedAt).toLocaleString()}`
        + (file?.status === 'sample' ? ' · SAMPLE DATA' : file?.status === 'carried-over' ? ' · carried over' : '');
    setBadge(status, note);
    logLine(`${status.padEnd(7)} px ${px(m.clean)}  YTM ${pct(m.ytm)}  HPR ${pct(m.hpr)}${why ? `  (${why})` : ''}`, status);
  } catch (e) {
    showError((e as Error).message);
    clearMetrics();
    setBadge('ERROR', (e as Error).message);
  }
}

/** Arm the next refresh. setTimeout (not setInterval) so a slow refresh never overlaps the next. */
function schedule(): void {
  window.clearTimeout(timer);
  const s = Math.max(5, Number(input('intervalS').value) || 60);
  nextAt = Date.now() + s * 1000;
  timer = window.setTimeout(async () => { await tick(); schedule(); }, s * 1000);
}

/** Refresh immediately and restart the countdown. */
function runNow(): void {
  void tick();
  schedule();
}

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------
/** Load the price file, restore the last inputs (or pick a 10-year note), wire the
 *  form events and start the loop. Form edits refresh after a 300 ms pause in typing. */
async function init(): Promise<void> {
  setBadge('LOADING', 'Loading Treasury prices…');
  let file: PriceFile | null = null;
  try {
    file = await loadPriceFile();
    fillDatalist(file);
  } catch (e) {
    showError(`Could not load Treasury prices (${(e as Error).message}). Manual prices still work.`);
  }
  if (file?.status === 'sample') $('sampleNote').hidden = false;

  const saved = loadState();
  const start: FormState = saved ?? (file
    ? { ...WORKED_EXAMPLE, source: 'fedinvest', cusip: defaultCusip(file), settleDate: '',
        purchaseSettle: '', purchaseClean: '' }
    : WORKED_EXAMPLE);
  writeForm(start);
  if (start.source === 'fedinvest') applyTreasury(start.cusip);

  let debounce: number | undefined;
  $('form').addEventListener('input', (ev) => {
    const t = ev.target as HTMLElement;
    if (t.id === 'cusip') applyTreasury((t as HTMLInputElement).value.trim());
    if (t.getAttribute('name') === 'source') {
      syncSourceUi();
      if (!$<HTMLInputElement>('srcManual').checked) applyTreasury(input('cusip').value);
    }
    window.clearTimeout(debounce);
    debounce = window.setTimeout(runNow, 300);
  });
  $('form').addEventListener('submit', (ev) => { ev.preventDefault(); runNow(); });
  $('refresh').addEventListener('click', runNow);
  $('example').addEventListener('click', () => { writeForm(WORKED_EXAMPLE); runNow(); });
  window.setInterval(() => {
    const left = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
    $('countdown').textContent = `next refresh in ${left}s`;
  }, 1000);
  runNow();
}

void init();
