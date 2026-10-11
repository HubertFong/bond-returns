# Developer guide

This guide is for engineers who will run, maintain or extend Bond Returns. For what the app does, see the [README](../README.md). For the finance, see the [method](METHOD.md).

---

## 1. Architecture

```
 ┌──────────────── GitHub Actions: .github/workflows/pages.yml ────────────────┐
 │  Python checks ─► fetch_fedinvest.py ─► npm test ─► npm run build ─► deploy │──► smoke test
 └──────────────────────────────┬──────────────────────────────────────────────┘
          FedInvest form (HTML) │ public/data/treasury_prices.json
                                ▼
                      GitHub Pages (static files)
                                │  index.html, assets/*.js|css, data/treasury_prices.json
                                ▼
 Browser:  main.ts ──► feed.ts getLivePrice() ──► bond.ts computeMetrics() ──► DOM
```

The app is a **static site with no backend**:

- **Prices** are collected by a scheduled CI job and deployed as a JSON file next to the page.
- **The maths** (yield solving, returns) runs in the browser.

This design means:

- **Free hosting:** GitHub Pages and GitHub Actions.
- **No server, database or API key** to run, secure or pay for.
- **No cross-origin problems:** FedInvest is a web form, which a browser could not call directly. CI reads it on the server side instead.
- **Reproducible results:** the same maths runs in Python and TypeScript and is cross-checked.

| Component | File | Responsibility |
|---|---|---|
| Bond maths | `src/bond.ts` | Dates, coupon schedule, day counts, accrued interest, pricing, YTM solver, returns. Pure functions, no I/O |
| Data access | `src/feed.ts` | `getLivePrice()`, the single entry point for prices; the price-file types; staleness rules |
| Page | `src/main.ts`, `index.html`, `src/style.css` | Form, CUSIP picker, refresh loop, rendering. Element ids are the contract between the HTML and `main.ts` |
| Price download | `python/fetch_fedinvest.py` | Fills in the FedInvest form, parses the price table, writes the JSON, falls back safely |
| Reference maths and CLI | `python/bond_returns.py` | The original implementation, a validation suite and a command-line monitor |
| Pipeline | `.github/workflows/pages.yml` | Test, fetch, build, deploy and smoke-test on push, on a schedule or on demand |

## 2. Repository map

```
.
├── index.html                    page markup (Vite entry point)
├── src/
│   ├── bond.ts                   bond maths (port of python/bond_returns.py §2–4)
│   ├── feed.ts                   getLivePrice(), price-file types, staleness
│   ├── main.ts                   page wiring and refresh loop
│   └── style.css                 styles; colour tokens with a dark theme
├── public/data/treasury_prices.json   sample price file for local work (CI replaces it)
├── test/
│   ├── bond.test.ts              Vitest suite
│   └── golden.json               962 cases computed by the Python reference
├── python/
│   ├── bond_returns.py           reference maths, validation, CLI monitor
│   ├── fetch_fedinvest.py        FedInvest download → JSON
│   ├── make_golden.py            regenerates test/golden.json
│   └── tests/test_fetch_fedinvest.py
├── docs/                         USER_GUIDE, METHOD, DEVELOPER, screenshot
├── .github/workflows/pages.yml   CI/CD
├── package.json, package-lock.json, tsconfig.json, vite.config.ts
├── CHANGELOG.md, LICENSE, README.md
```

## 3. Local setup

**Requirements:**

- Node.js 22 with npm.
- Python 3.9 or later (CI uses 3.12).
- `scipy` is optional. Without it, the Python solver falls back to bisection.

```bash
npm ci                     # install the exact dependency versions in package-lock.json
npm run dev                # dev server with hot reload: http://localhost:5173/bond-returns/
npm test                   # Vitest suite
npm run build              # type check (tsc) + production build into dist/
npm run preview            # serve dist/ at http://localhost:4173/bond-returns/

pip install scipy
python python/bond_returns.py --validate
python -m unittest discover -s python/tests
python python/fetch_fedinvest.py --out public/data/treasury_prices.json   # real prices
```

Local runs read `public/data/treasury_prices.json`. The committed copy is a **sample** with illustrative prices: its `status` is `"sample"`, and the page says so in a banner. Downloading real prices overwrites it. Don't commit real prices: CI downloads fresh ones on every deploy.

## 4. Tests

| Suite | Command | What it proves |
|---|---|---|
| TypeScript maths | `npm test` | Excel `PRICE`/`YIELD` documentation examples. The worked example. Price↔yield round trips for every day count and frequency. Zero coupons, settlement on a coupon date, negative yields, the solver fallback, month-end schedules, staleness. **Plus 962 cases that must match the Python reference to 1e-9** |
| Type check | `npm run build` | `tsc --noEmit` in strict mode, then the production build |
| Python reference | `python python/bond_returns.py --validate` | 14 checks: the same published examples and edge cases, plus the CLI's live loop fed a scripted sequence of good, missing, bad, stale and failing quotes |
| Price download | `python -m unittest discover -s python/tests` | Form detection and filling (CSRF token, ISO date), table parsing, coupon units, header-mapped columns, refusal of a changed format, and the weekend/holiday date sequence |

**The cross-check.** `test/golden.json` holds 962 cases computed by `python/bond_returns.py`. They cover every day count, every frequency, five maturities (including month-end and February), four settlement dates, coupon and zero-coupon bonds, and a range of prices. Regenerate the file whenever the maths changes intentionally:

```bash
python python/make_golden.py && npm test
```

A change to the bond maths always goes into **both** `python/bond_returns.py` and `src/bond.ts`. The cross-check fails if only one is changed.

## 5. The price pipeline

`python/fetch_fedinvest.py` collects the prices. It uses the standard library only.

1. **Load the form.** It GETs `https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate` with browser headers. The response sets a session cookie, and its form has one date field (`priceDate`, `YYYY-MM-DD`), a submit button and a hidden `_csrf` token.
2. **Submit it** for a date with the same session. A submission without the token gets HTTP 403.
3. **Parse the result table.** The columns are CUSIP, security type, rate, maturity, call date, buy, sell and end of day. It keeps `MARKET BASED NOTE` and `MARKET BASED BOND` rows. Coupons are printed as bare percents (`4.250`), so the units are decided for the whole table at once. That way `0.125` is read as 0.125%, not 12.5%. A coupon above 25% means the format has changed, and the parse is refused.
4. **Choose the date.** It tries today in New York first, then earlier weekdays, up to 7. A weekend, holiday or not-yet-published day returns no table and is skipped. Each date gets a fresh session and token.
5. **Write** `public/data/treasury_prices.json`:

```jsonc
{
  "source": "US Treasury FedInvest end-of-day prices",
  "status": "fresh",                      // or "carried-over" or "sample"
  "price_date": "2026-10-09",
  "fetched_at": "2026-10-11T00:51:19+00:00",
  "securities": [
    {"cusip": "91282CLW9", "type": "MARKET BASED NOTE", "rate": 0.0425,
     "maturity": "2034-11-15", "buy": 99.21875, "sell": 99.1875, "eod": 99.203125}
  ]
}
```

**On failure, the site never breaks:**

1. If no date yields prices, the file currently on GitHub Pages is downloaded and reused as `"carried-over"`.
2. If that is unavailable too, the existing file is left as it is.
3. Either way the step succeeds with a `::warning::`, the deploy goes ahead, and the page shows `STALE` once the prices age.

## 6. Deployment

**One-time setting:** in the repository, go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**. This is already set for `HubertFong/bond-returns`.

**When the workflow runs:**

- on every push to `main`;
- on weekdays at 18:47 and 22:47 UTC;
- on demand from **Actions → Build and deploy → Run workflow**, with an optional `start_date` to begin the fetch from a chosen day;
- on pull requests, where it tests and builds but doesn't deploy.

**Steps:**

1. Python checks.
2. Fetch prices.
3. `npm ci`, `npm test` and `npm run build`.
4. Upload `dist/`.
5. Deploy to Pages.
6. Smoke test: load the live page and price file, log the price date and row count, and warn unless the status is `fresh`.

**What blocks a deploy, and what only warns:**

- **Blocks:** a failing test, a type error or a failed build. The site keeps its previous version.
- **Only warns:** a failed price fetch, or a live site without fresh prices.

**Permissions:** the workflow needs only `contents: read`, `pages: write` and `id-token: write`. It has no secrets.

## 7. Operations runbook

| Symptom | Likely cause | What to do |
|---|---|---|
| Badge shows `STALE` on a weekday evening | Today's run failed or hasn't happened | Open **Actions** and check the latest run. Use **Run workflow** to retry |
| Warning: "FedInvest fetch failed; carrying over…" | FedInvest down, slow or refusing requests | Usually temporary; the next scheduled run retries. The log shows the HTTP status and the start of the error page |
| Warning: "FedInvest page layout changed" | Treasury changed the form | The page HTML is printed in a folded log group. Update `parse_form()` / `form_payload()` and the fixture in `python/tests/test_fetch_fedinvest.py` |
| Error: "RATE column does not look like a coupon" | The table format changed | Inspect the folded page in the log, then adjust `parse()` and its tests |
| Scheduled runs stopped | GitHub pauses schedules after 60 days without repository activity | **Actions → Build and deploy → Enable workflow**, or push any commit |
| Smoke test fails: page not found | The Pages source is not set to GitHub Actions, or the deploy failed | See §6, then re-run the workflow |
| Warning about Node.js 20 in GitHub's Pages actions | Comes from GitHub's own `upload-artifact` and `deploy-pages` actions | Harmless; it runs on Node 24. Bump the action versions when new majors are released |
| `STALE` the morning after a US holiday | The page's calendar knows weekends only | Expected; it clears with the next business day's prices. See Extending to add holidays |

To test the holiday fallback on purpose, run the workflow by hand with `start_date` set to a holiday. The log should show that date with 0 rows, followed by the previous business day.

## 8. Extending

**Add a price source** (a broker API or a paid feed):

- **Web page:** add a variant to `FeedConfig` and a branch in `getLivePrice()` in `src/feed.ts` that returns a `Quote`, or `null` when there is no price. Nothing else changes. Browsers need a source that allows cross-origin requests; otherwise fetch it in CI the way FedInvest is.
- **Python:** add a branch in `get_live_price()` in `python/bond_returns.py`.

**Change the maths.** Edit `python/bond_returns.py` and `src/bond.ts` together. Run `python python/make_golden.py`, then run every suite in §4.

**Add a holiday calendar.** Business days are computed in four places:

- `addBusinessDays()` and `previousBusinessDay()` in `src/bond.ts`;
- `add_business_days()` in `python/bond_returns.py`;
- `business_days_back()` in `python/fetch_fedinvest.py`.

A shared list of US federal (or SIFMA) holidays used by all of them would remove the post-holiday `STALE` badge.

**Support more instruments.** Bills, TIPS, FRNs and callable bonds need their own pricing conventions. FedInvest already publishes bills, TIPS and FRNs; the fetcher filters them out in `KEEP_TYPES`.

## 9. The Python command-line monitor

`python/bond_returns.py` is the reference implementation, and also a polling monitor for the terminal.

```bash
python python/bond_returns.py --example                      # the worked example, step by step
python python/bond_returns.py --validate                     # 14 checks; exit code 1 on failure
python python/bond_returns.py --source mock --interval 1 --iterations 10
python python/bond_returns.py --source fedinvest --ticker 91282CLW9 --interval 300
python python/bond_returns.py --config my_bond.json --source bloomberg --ticker "US91282CLW90 Govt"
```

**Code layout**, by section of the file:

| § | Contents |
|---|---|
| 1 | `Bond`, `Position` and `FeedConfig`: every parameter, overridable from JSON (`--config`) and the command line |
| 2 | `coupon_dates`, `days_30_360` and `settle_state`: schedule, day counts, accrued interest |
| 3 | `dirty_price` and `solve_ytm`: pricing, with Newton-Raphson falling back to brentq |
| 4 | `compute_metrics`: current yield, YTM and holding-period return |
| 5 | `get_live_price` and the sources: mock, fedinvest, and the Bloomberg/Refinitiv templates |
| 6 | `LiveMonitor`: the polling loop |
| 7 | `worked_example` and `run_validation` |
| 8 | The command line |

**Configuration file** (every key is optional; dates are ISO strings):

```json
{"bond":     {"face_value": 1000, "coupon_rate": 0.0425, "frequency": 2,
              "maturity": "2034-11-15", "day_count": "ACT/ACT"},
 "position": {"purchase_settle": "2025-03-03", "purchase_clean": 98.25},
 "feed":     {"source": "fedinvest", "ticker": "91282CLW9", "interval_s": 300,
              "stale_after_s": 345600, "settle_lag_days": 1}}
```

With `--source fedinvest` and no `bond` in the config, the Treasury's terms are filled in from the price file. With `kind: "yield"`, the source supplies a yield; it is converted to a clean price at the current settlement date.

**The live loop.** Each tick works out the settlement date and fetches a quote. It then rejects the quote if any of these hold:

- it isn't a number;
- the price is outside (0, 1000);
- its timestamp is in the future;
- it jumps more than `max_jump_pct`, unless the jump repeats `max_rejects` times in a row.

A rejected or missing quote is replaced by the last good quote while that is younger than `max_reuse_s`. Quotes older than `stale_after_s` are flagged. The YTM is then recomputed, starting from the last result, and one line is logged:

```
INFO  OK      px 98.5000 age 0.0s | settle 2026-10-12 AI 1.9500 dirty 100.4500 | CY 6.0914% YTM 6.6164% (newton/3) | HPR 8.0711% ann 7.4948% (392d)
```

**Bloomberg and Refinitiv are untested templates.** They were written against the vendors' documented APIs but never run against a live terminal. Before relying on them, check the security identifiers, field names and entitlements:

- **Bloomberg** (`blpapi`): a `ReferenceDataRequest` for `PX_LAST` (price) or `YLD_YTM_MID` (yield), timestamped from `LAST_UPDATE_DT` and `LAST_UPDATE`. For tick data, subscribe to `//blp/mktdata` instead.
- **Refinitiv** (`lseg-data` or `refinitiv-data`): `get_data` for `CF_LAST` or `RED_YIELD`, timestamped from `CF_DATE` and `CF_TIME`.

## 10. Conventions

- **No framework.** The page is plain TypeScript and the DOM. Dependencies are development-only: TypeScript, Vite and Vitest.
- **The fetcher uses the standard library only**, so CI needs nothing but Python.
- **Comments explain *why*:** each module opens with its role and reading order, and the key formulas are commented where they are implemented.
- **Units:** prices per 100 of face value, rates as decimals, dates as ISO strings at the edges and day numbers inside `bond.ts`.
