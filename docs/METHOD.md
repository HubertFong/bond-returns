# Method: how the returns are calculated

This note is for analysts and anyone checking the numbers. It explains the method step by step, then works one bond through by hand:

- the formulas: cash flows, accrued interest, clean and dirty price, yield to maturity, current yield and holding-period return;
- a worked example you can follow with a calculator;
- the validation against published values;
- the edge cases.

The same maths runs in two places, and the tests check that they agree on 962 cases:

- **`python/bond_returns.py`**: the Python reference.
- **`src/bond.ts`**: the TypeScript port the web page uses.

To reproduce the worked example and the validation yourself:

```
python python/bond_returns.py --example     # section 8, printed step by step
python python/bond_returns.py --validate    # section 9's checks
```

---

## 1. Notation

| Symbol | Meaning |
|---|---|
| `c` | annual coupon rate (0.06 = 6%) |
| `f` | coupons per year (1, 2, 4, 12) |
| `C = 100·c/f` | coupon per period, per 100 face |
| `R` | redemption per 100 face (normally 100) |
| `s` | settlement date (trade date + T+1 or T+2 business days) |
| `L`, `Nc` | last coupon date ≤ s, next coupon date > s |
| `N` | number of coupons still to be paid after s |
| `y` | yield to maturity, annual, compounded `f` times a year |

All prices are per 100 of face value. The face value in the config (for example 1,000) only converts prices into currency amounts.

## 2. Cash-flow schedule

Coupon dates are rolled **back from maturity** in steps of `12/f` months:

```
T_k = maturity − k·(12/f) months,   k = 0, 1, 2, ...
```

Each date is computed from maturity itself, not from the previous date, so a 31st doesn't drift to the 30th. If maturity is a month-end, every coupon date is a month-end (31 Aug → 28 Feb → 31 Aug).

The remaining cash flows are `C` on each coupon date after `s`, and `C + R` at maturity. A coupon paid on date `d` goes to whoever holds the bond on `d`. If you settle on `d`, the seller keeps that coupon.

## 3. Day count and accrued interest

The buyer pays the seller the coupon earned since the last payment date:

```
AI = C · A / E
```

| Convention | A (accrued days) | E (days in period) | DSC (days to next coupon) |
|---|---|---|---|
| 30/360 (US) | 30/360 days from L to s | 360/f | E − A |
| ACT/ACT (ICMA) | actual days from L to s | actual days from L to Nc | actual days from s to Nc |
| ACT/365 (Fixed) | actual days from L to s | 365/f | actual days from s to Nc |

30/360 day count: `360·(Y2−Y1) + 30·(M2−M1) + (D2−D1)`, after these adjustments:

- If D1 = 31, set D1 = 30.
- If D2 = 31 and D1 ≥ 30, set D2 = 30.
- For month-end bonds, the last day of February counts as day 30.

ACT/ACT here is the ICMA bond convention (US Treasuries, most government bonds). It is not the ACT/ACT ISDA convention used for swaps.

## 4. Clean vs dirty price

```
P_dirty = P_clean + AI
```

Screens quote the clean price because it doesn't jump on coupon dates. You actually pay the dirty price (also called invoice or full price). Both the YTM equation and the return calculation must use the dirty price.

## 5. YTM equation

YTM is the single rate that discounts the remaining cash flows to the dirty price:

```
P_dirty(y) = Σ_{k=1..N}  CF_k / (1 + y/f)^(w + k − 1)        w = DSC / E
```

`w` is the fraction of a period left until the next coupon. This is the "street" convention used by Excel PRICE/YIELD, the SIA and ICMA.

When only one cash flow is left, the market convention is simple interest: `P_dirty = (C + R) / (1 + w·y/f)`. This is the default (`simple_last_period` / `simpleLastPeriod`).

There is no closed form when N > 1, so the code solves `P_dirty(y) − (P_clean,market + AI) = 0` numerically:

- **Start value:** `y0 ≈ (100c + (R − P)/T) / ((R + P)/2)`, where `T` is years to maturity. In the live loop, the previous tick's YTM is used instead.
- **Newton-Raphson:** `y ← y − (P(y) − P*) / P′(y)`, where `P′(y) = −Σ CF_k · (t_k/f) · (1+y/f)^(−t_k−1)` and `t_k = w + k − 1`. It usually converges in 1 to 3 iterations.
- **Fallback:** if a Newton step diverges, leaves the formula's domain (`y > −f`, or `y > −f/w` with one simple-interest period left), or hits a zero slope, the code brackets the root and uses Brent's method: `scipy.optimize.brentq` in Python (bisection if scipy is missing), its own implementation in TypeScript. The price falls steadily as the yield rises, so any positive price has exactly one yield. Negative yields are included.

## 6. Current yield

```
CY = annual coupon / clean price = 100·c / P_clean
```

Current yield is income only. It ignores the pull to par and time value, and it is 0 for a zero-coupon bond.

## 7. Holding-period return (HPR)

```
HPR = (P_dirty,now + coupons received − P_dirty,paid) / P_dirty,paid
    = (Δ clean price + Δ accrued + coupons) / P_dirty,paid

annualised (compound, ACT/365):  (1 + HPR)^(365/days) − 1
```

- **Coupons counted:** those paid in the window (purchase settlement, current settlement]. The start date is excluded and the end date included.
- **Reinvestment:** coupons are not reinvested.
- **Mark-to-market:** if you still hold the bond, the return is unrealised.
- **Short holdings:** annualising a holding of a few days makes noise look like a return, so read the raw HPR instead.

## 8. Worked example by hand

**Bond:** 6% semi-annual, matures 15 Jun 2029, 30/360, face 1,000.
**Bought:** settled 15 Sep 2025 at a clean price of 97.00.
**Today:** settles 12 Oct 2026 (T+1 from Saturday 10 Oct 2026) at a live clean price of 98.50.

**Step 1: Schedule.** Coupon dates fall on 15 Jun and 15 Dec. `L` = 15 Jun 2026 and `Nc` = 15 Dec 2026. Six cash flows remain, each `C` = 3.00, with 103.00 at maturity.

**Step 2: Accrued interest at purchase.** `A` = 30/360 days from 15 Jun 2025 to 15 Sep 2025 = 90.

- AI = 3 × 90/180 = **1.50**
- Dirty paid = 97.00 + 1.50 = **98.50**

**Step 3: Accrued interest now.**

- `A` = 360·0 + 30·(10 − 6) + (12 − 15) = **117**
- `E` = 180
- `DSC` = 180 − 117 = 63, so `w` = 0.35
- AI = 3 × 117/180 = **1.95**
- Dirty = 98.50 + 1.95 = **100.45**

**Step 4: YTM.** To do it by hand, price the bond at two trial yields and interpolate.

| Date | CF | t (periods) | DF at 6.6% (r = 1.033) | PV | DF at 6.7% (r = 1.0335) | PV |
|---|---:|---:|---:|---:|---:|---:|
| 15 Dec 26 | 3.00 | 0.35 | 0.988701 | 2.9661 | 0.988533 | 2.9656 |
| 15 Jun 27 | 3.00 | 1.35 | 0.957116 | 2.8713 | 0.956491 | 2.8695 |
| 15 Dec 27 | 3.00 | 2.35 | 0.926540 | 2.7796 | 0.925487 | 2.7765 |
| 15 Jun 28 | 3.00 | 3.35 | 0.896941 | 2.6908 | 0.895488 | 2.6865 |
| 15 Dec 28 | 3.00 | 4.35 | 0.868288 | 2.6049 | 0.866462 | 2.5994 |
| 15 Jun 29 | 103.00 | 5.35 | 0.840549 | 86.5766 | 0.838376 | 86.3527 |
| **Dirty** | | | | **100.4894** | | **100.2501** |

The target dirty price of 100.45 lies between the two:

```
y ≈ 6.6% + 0.1% × (100.4894 − 100.45) / (100.4894 − 100.2501) = 6.6165%
```

Newton in the code, starting from the approximation y0 = 6.6104%:

| Iteration | y | P_dirty(y) | P − target |
|---:|---:|---:|---:|
| 1 | 6.610350% | 100.464559 | 1.5e-2 |
| 2 | 6.616429% | 100.450001 | 1.3e-6 |
| 3 | 6.616430% | 100.450000 | 1.0e-13 |

**YTM = 6.6164%.**

**Step 5: Current yield.** CY = 6 / 98.50 = **6.0914%**

**Step 6: Holding-period return.** Two coupons were received in the window: 15 Dec 2025 and 15 Jun 2026, totalling 6.00.

- Clean change = 98.50 − 97.00 = +1.50
- Accrued change = 1.95 − 1.50 = +0.45
- Coupons = +6.00
- Total gain = **7.95**
- HPR = 7.95 / 98.50 = **8.0711%**
- Annualised = 1.080711^(365/392) − 1 = **7.4948%** (over 392 days)

## 9. Validation

`python python/bond_returns.py --validate` runs these checks, and all of them pass:

| Check | Expected | Got |
|---|---|---|
| Microsoft Excel `YIELD` documentation example: settles 15-Feb-08, matures 15-Nov-16, 5.75%, price 95.04287, semi-annual, 30/360 | 6.5% | 6.500001% (the input price is rounded to 5 decimals) |
| Microsoft Excel `PRICE` documentation example: same bond, matures 15-Nov-17, yield 6.5% | 94.63436 | 94.63436 |
| Worked example in section 8 | AI 1.95, HPR 8.0711% | same |
| Price → yield → price for every day count × frequency (1, 2, 4, 12) × yields from −2% to +40% | exact | max error 1.7e-13 |
| Zero-coupon bond against the closed form 100/(1+y/2)^9 | 4.000000% | 4.000000% |
| Settlement on a coupon date; negative yield; simple vs compound last period; a yield below −f in a simple last period; Newton → brentq fallback; month-end schedule; matured bond refused | | pass |
| Live loop fed a scripted sequence (good, missing, NaN, 18% jump, 5-minute-old quote, exception) | OK, REUSED, REUSED, REUSED, STALE, NO DATA | same |

To check a real bond against Bloomberg `YAS` or an online calculator:

1. Enter the same settlement date, clean price and day count.
2. Compare with the **Street Convention** yield. Bloomberg's "True Yield" also shifts payment dates for holidays, which this code does not do.
3. The yields should agree to about 1e-6. A gap of a few basis points usually means a different settlement date, a different day count, or an odd first coupon.

## 10. Edge cases

**Zero-coupon bonds.** Set `coupon_rate=0`. AI = 0 and CY = 0. YTM is compounded at `frequency`: use 2 for US STRIPS and 1 for most euro zeros. The schedule is still used to measure time in periods.

**Settlement on a coupon date.** A = 0 and AI = 0. `w` = 1, so the next coupon is a full period away. That day's coupon goes to the seller and is excluded from both future cash flows and the buyer's coupons received.

**Negative yields.** These occur when the price is above the undiscounted sum of the remaining cash flows. The solver accepts any yield in the formula's domain: `y > −f` normally, and `y > −f/w` in a simple-interest last period. Its bracket walks down towards that floor.

**One cash flow left.** Excel/SIA simple interest is used. Set `simple_last_period=False` (Python) or `simpleLastPeriod: false` (TypeScript) to keep compounding instead; the two differ by about 0.5 bp in the test.

**Matured bond.** A settlement on or after maturity is refused with a clear message.

**Bad prices.** A price ≤ 0 or NaN is rejected before it reaches the solver.

**Not modelled:**

- Odd first or last coupons: bonds still in their first, irregular period.
- Ex-coupon periods (UK gilts, Australian bonds).
- Holiday calendars.
- Coupon reinvestment, taxes and fees.
- Amortising, callable and floating-rate bonds.
