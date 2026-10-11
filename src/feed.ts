/**
 * feed.ts - where prices come from, and how fresh they are.
 *
 * Every price reaches the page through one function, getLivePrice(), mirroring
 * get_live_price() in python/bond_returns.py. Adding a source (a broker API, a paid
 * feed) means adding a branch there; nothing else in the page changes.
 *
 * The built-in source is the US Treasury's FedInvest end-of-day price list. The page
 * does not call FedInvest itself (it is a web form, and browsers block cross-site
 * requests to it). Instead the GitHub Actions workflow runs python/fetch_fedinvest.py
 * each weekday, writes data/treasury_prices.json, and deploys it with the site. The
 * page then reads that same-origin file.
 */

import { type Day, parseISO, previousBusinessDay } from './bond';

/** One Treasury note or bond in the price file. */
export interface TreasuryRow {
  cusip: string;
  type: string;      // MARKET BASED NOTE | MARKET BASED BOND
  rate: number;      // annual coupon, decimal
  maturity: string;  // YYYY-MM-DD
  buy: number | null;
  sell: number | null;
  eod: number;       // end-of-day clean price per 100
}

/**
 * data/treasury_prices.json. `status` says where the prices came from:
 *   fresh         fetched from FedInvest by the latest workflow run
 *   carried-over  the fetch failed, so the previously deployed prices were kept
 *   sample        the illustrative file in the repo (local development only)
 */
export interface PriceFile {
  source: string;
  status: 'fresh' | 'carried-over' | 'sample';
  price_date: string;  // business day the prices are for
  fetched_at: string;  // ISO timestamp of the fetch
  securities: TreasuryRow[];
}

/** A clean price per 100 and the business day it is for. */
export interface Quote {
  clean: number;
  priceDate: Day;      // as-of date of the price
  fetchedAt: string;
  source: string;
}

/** Which source to ask: a Treasury by CUSIP, or a price typed in by the user. */
export type FeedConfig =
  | { source: 'fedinvest'; cusip: string; url?: string }
  | { source: 'manual'; price: number; today: Day };

/** The price file, relative to the site root (Vite's base path, /bond-returns/). */
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

/** The last price file loaded, for the CUSIP picker. */
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

/**
 * The badge at the top of the page:
 *   OK       a fresh quote, current for an end-of-day source
 *   STALE    the quote is older than the previous business day
 *   REUSED   this refresh failed, so the last good quote is shown again
 *   NO DATA  nothing usable, so nothing is computed
 */
export type Status = 'OK' | 'STALE' | 'REUSED' | 'REUSED+STALE' | 'NO DATA';

/**
 * End-of-day data is current if it is for the previous business day or later: on a
 * Monday, Friday's prices are fine. (Weekends only; after a public holiday the badge can
 * read STALE until the next business day's prices are published.)
 */
export const isStale = (priceDate: Day, today: Day): boolean => priceDate < previousBusinessDay(today);

/** Reject values that cannot be a clean price. Returns the reason, or null if fine. */
export function quoteProblem(q: Quote): string | null {
  if (!Number.isFinite(q.clean)) return 'non-numeric price';
  if (!(q.clean > 0 && q.clean < 1000)) return `price ${q.clean} out of range`;
  return null;
}
