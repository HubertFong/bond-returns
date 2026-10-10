// Market data. Everything goes through getLivePrice(), like get_live_price() in
// python/bond_returns.py, so a different or mock source is a one-function swap.
//
// Source: US Treasury FedInvest official end-of-day prices, fetched by the
// GitHub Actions workflow (python/fetch_fedinvest.py) into data/treasury_prices.json
// and deployed with the site. Free, no key, updated once per business day.

import { type Day, parseISO, previousBusinessDay } from './bond';

export interface TreasuryRow {
  cusip: string;
  type: string;      // MARKET BASED NOTE | MARKET BASED BOND
  rate: number;      // annual coupon, decimal
  maturity: string;  // YYYY-MM-DD
  buy: number | null;
  sell: number | null;
  eod: number;       // end-of-day clean price per 100
}

export interface PriceFile {
  source: string;
  status: 'fresh' | 'carried-over' | 'sample';
  price_date: string;  // business day the prices are for
  fetched_at: string;  // ISO timestamp of the fetch
  securities: TreasuryRow[];
}

export interface Quote {
  clean: number;
  priceDate: Day;      // as-of date of the price
  fetchedAt: string;
  source: string;
}

export type FeedConfig =
  | { source: 'fedinvest'; cusip: string; url?: string }
  | { source: 'manual'; price: number; today: Day };

export const DATA_URL = `${import.meta.env.BASE_URL}data/treasury_prices.json`;

let lastFile: PriceFile | null = null;

/** Download and sanity-check the price file. Throws on network or format errors. */
export async function loadPriceFile(url = DATA_URL): Promise<PriceFile> {
  const res = await fetch(`${url}?t=${Date.now()}`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`price file: HTTP ${res.status}`);
  const file = (await res.json()) as PriceFile;
  if (!Array.isArray(file.securities) || typeof file.price_date !== 'string') {
    throw new Error('price file: unexpected format');
  }
  lastFile = file;
  return file;
}

export const cachedPriceFile = (): PriceFile | null => lastFile;

/** THE single entry point for market data: a Quote, or null if there is no value. */
export async function getLivePrice(cfg: FeedConfig): Promise<Quote | null> {
  if (cfg.source === 'manual') {
    return Number.isFinite(cfg.price) && cfg.price > 0
      ? { clean: cfg.price, priceDate: cfg.today, fetchedAt: new Date().toISOString(), source: 'manual' }
      : null;
  }
  const file = await loadPriceFile(cfg.url);
  const row = file.securities.find((s) => s.cusip === cfg.cusip);
  if (!row || !(row.eod > 0)) return null;
  return { clean: row.eod, priceDate: parseISO(file.price_date), fetchedAt: file.fetched_at, source: file.source };
}

export type Status = 'OK' | 'STALE' | 'REUSED' | 'REUSED+STALE' | 'NO DATA';

/** End-of-day data is current if it is for the previous business day or later. */
export const isStale = (priceDate: Day, today: Day): boolean => priceDate < previousBusinessDay(today);

/** Reject values that cannot be a clean price. Returns the reason, or null if fine. */
export function quoteProblem(q: Quote): string | null {
  if (!Number.isFinite(q.clean)) return 'non-numeric price';
  if (!(q.clean > 0 && q.clean < 1000)) return `price ${q.clean} out of range`;
  return null;
}
