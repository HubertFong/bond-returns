# Bond Returns

**Live site:** https://hubertfong.github.io/bond-returns/

Return metrics for a fixed-coupon bond, recalculated from free official US Treasury prices:

- accrued interest, clean and dirty price
- current yield
- yield to maturity (Newton-Raphson, falling back to Brent's method)
- holding-period return since your purchase, raw and annualised

Pick a Treasury note or bond by CUSIP and its coupon, maturity and latest price fill in. Or switch to **Manual price** and enter any fixed-coupon bond. The page re-fetches and recomputes every N seconds (60 by default). The settlement date rolls forward with the calendar, so accrued interest stays current.

## Data source

[US Treasury FedInvest](https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate) publishes official **end-of-day** clean prices for every marketable Treasury security each business day. It is free and needs no API key.

These are not intraday quotes; free live bond quotes are effectively unavailable. The status badge shows how current the data is:

| Badge | Meaning |
|---|---|
| `OK` | Prices are from the previous business day or later |
| `STALE` | Prices are older than that |
| `REUSED` | This refresh failed and the last good price was used |
| `NO DATA` | Nothing usable was available |

How the prices get to the page:

1. A GitHub Actions workflow (`.github/workflows/pages.yml`) runs on weekdays at 18:47 and 22:47 UTC, and on every push.
2. It runs `python/fetch_fedinvest.py`. The script submits FedInvest's "Historical Prices" form for the latest business day, reads the price table (about 330 notes and bonds), keeps only fixed-coupon notes and bonds, and writes `data/treasury_prices.json`.
3. The workflow deploys that file with the site, and the page reads it through one function, `getLivePrice()` in `src/feed.ts`.
4. If FedInvest is unreachable, the previously deployed file is carried over, so the page shows `STALE` instead of breaking.
5. After every deploy, a smoke test loads the live page and price file. It logs the price date and row count, and raises a warning if the prices are not fresh.

The `public/data/treasury_prices.json` file in the repo is an **illustrative sample** for local development and is labelled as such on the page.

## Layout

| Path | What |
|---|---|
| `src/bond.ts` | The bond maths: schedule, day counts (30/360, ACT/ACT ICMA, ACT/365), accrued interest, pricing, YTM solver, HPR. A port of the Python reference. |
| `src/feed.ts` | `getLivePrice()`: the single data entry point. FedInvest or manual price, plus staleness checks. |
| `src/main.ts`, `index.html`, `src/style.css` | The page. Plain TypeScript, no framework. |
| `test/bond.test.ts` | Vitest: the Excel `YIELD`/`PRICE` examples, the worked example, edge cases, and 962 cases cross-checked against Python to within 1e-9. |
| `python/bond_returns.py` | Python reference and CLI with mock, FedInvest, Bloomberg and Refinitiv sources. `--validate` runs the checks. |
| `python/fetch_fedinvest.py` | Downloads FedInvest prices to JSON. Standard library only. |
| `python/make_golden.py` | Regenerates `test/golden.json` from the Python reference. |
| `docs/METHOD.md` | Formulas, a worked example done by hand, validation and edge cases. |

## Run locally

```bash
npm ci
npm run dev          # http://localhost:5173/bond-returns/
npm test             # TypeScript tests
npm run build        # type-check and build to dist/

pip install scipy
python python/bond_returns.py --validate
python python/bond_returns.py --example
python python/bond_returns.py --source fedinvest --ticker 91282CLW9 --interval 300
python python/fetch_fedinvest.py --out public/data/treasury_prices.json   # real prices locally
```

## Limits

- **Not modelled:** odd first or last coupons, ex-coupon periods, holiday calendars (weekends only), coupon reinvestment and taxes.
- **Treasuries only for live prices:** FedInvest has no bills, TIPS or FRNs in this tool, and no corporate bonds. Use the manual price for anything else.
- **Prices are FedInvest's end-of-day valuations**, which may differ slightly from dealer quotes.

Not investment advice.
