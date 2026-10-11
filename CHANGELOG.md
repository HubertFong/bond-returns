# Changelog

## 1.0.0 (11 October 2026)

First release.

### Web app

- **Return figures:** yield to maturity, current yield, holding-period return and its annualised equivalent, accrued interest, clean and dirty price, market value, and the full cash-flow schedule with present values.
- **Treasury prices:** any US Treasury note or bond by CUSIP, with terms and the latest official end-of-day price filled in automatically.
- **Manual mode:** any fixed-coupon bond with a typed-in price. It supports 30/360, ACT/ACT (ICMA) and ACT/365 day counts, and 1, 2, 4 or 12 coupons a year.
- **Refresh:** automatic on a configurable interval, with a status badge (OK, STALE, REUSED, NO DATA) and an update log.
- **Inputs:** remembered in the browser. A one-click worked example is included.
- **Layout:** responsive for phone and desktop, with light and dark themes.

### Data

- **Price download:** official end-of-day prices from US Treasury FedInvest, collected on weekdays by GitHub Actions and published with the site.
- **Fallbacks:** weekends, holidays and days not yet published fall back to the latest available prices. If a download fails, the previous prices are kept and shown as stale.

### Python

- **Reference script:** `bond_returns.py`, the reference implementation and a command-line monitor with mock and FedInvest price sources.
- **Paid-feed templates:** Bloomberg and LSEG/Refinitiv connectors, clearly marked as untested.

### Quality

- **Published examples:** matches Microsoft Excel's `PRICE` and `YIELD` documentation examples.
- **Cross-check:** the Python and TypeScript implementations agree to within 1e-9 on 962 cases.
- **Tests:** 975 TypeScript tests, 10 price-download tests and 14 Python validation checks run on every change. A smoke test checks the live site after each deploy.
