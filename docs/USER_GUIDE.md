# User guide

Bond Returns works out what a fixed-coupon bond yields and what it has earned you so far. This guide goes through the page from top to bottom.

**Open the app:** https://hubertfong.github.io/bond-returns/

---

## 1. Choose a bond

### A US Treasury (the default)

Under **Price source**, keep **US Treasury (FedInvest)** selected and type in the **Treasury CUSIP** box. The list narrows as you type and shows each security's coupon and maturity, for example *4.250% note due 15 Nov 2034*.

When you pick one, the page fills in:

- the **coupon**, **maturity**, **coupons per year** (2) and **day count** (ACT/ACT);
- the latest **official end-of-day price**.

These fields are locked, because they come from the Treasury.

> **Where do I find a CUSIP?** It's the 9-character code on your broker statement or trade confirmation, for example `91282CLW9`. TreasuryDirect also lists every Treasury by CUSIP.

### Any other bond

Choose **Manual price** and fill in:

| Field | What to enter |
|---|---|
| Clean price (per 100) | The quoted price, as a percentage of face value: `98.50` means 98.50% of par |
| Face value | The amount per bond, e.g. `1000`. It is only used for the market value line |
| Coupon (%) | The annual coupon rate, e.g. `6` for 6% |
| Coupons per year | 2 for most US bonds, 1 for most European bonds, 4 or 12 for some others |
| Maturity | The final repayment date |
| Day count | The bond's convention for counting interest days (see below) |

**Which day count?** Check the bond's prospectus. As a rule of thumb:

| Bond | Day count |
|---|---|
| US Treasuries, most government bonds | ACT/ACT |
| US corporates, agencies, municipals | 30/360 |
| Sterling and some money-market-style bonds | ACT/365 |

## 2. Settlement

A bond trade settles a few business days after the trade date. Interest is counted up to the settlement date.

- **Lag (business days):** 1 for US Treasuries (T+1), which is the default, or 2 for many other bonds. The settlement date then moves forward with the calendar by itself.
- **Fixed date (optional):** set this to value the bond on a particular date. It overrides the lag. Leave it empty to follow today.

## 3. Your purchase

To see your return, enter:

- **Settlement date:** the date your purchase settled.
- **Clean price paid:** the price per 100 on your contract note, *without* accrued interest.

Leave these blank if you only want yields.

## 4. Reading the results

### The four headline numbers

| Result | What it means |
|---|---|
| **Yield to maturity** | The annual return you'd earn by buying at today's price and holding to maturity, with coupons reinvested at the same rate. "Street convention" is the market standard, the same as Excel's `YIELD` |
| **Current yield** | The annual coupon divided by the price. It counts income only and ignores any gain or loss to maturity |
| **Holding-period return** | What you've made since you bought, as a percentage of what you paid. It includes the price change, interest accrued and coupons received. It is not annualised |
| **HPR annualised** | The same return scaled to one year. Over less than about a month this exaggerates noise, so read the plain HPR instead |

### Clean and dirty prices, in one paragraph

Bond prices are quoted **clean**, without the interest that has built up since the last coupon. When you buy, you also pay the seller that **accrued interest**, so the amount you actually pay is the **dirty** price: clean plus accrued. The page shows all three, and uses the dirty price for every return, because that's the money that changes hands.

### The details table

| Row | Meaning |
|---|---|
| Settlement date | The date the figures are calculated for |
| Clean price | The quoted price per 100 |
| Accrued interest | Interest built up since the last coupon, per 100, with the day count used |
| Last / next coupon | The coupon dates either side of settlement |
| Dirty price | Clean plus accrued: what a buyer pays per 100 |
| Market value | The dirty value of one bond of your face value |
| Coupons received | Coupons paid to you since your purchase settled, per 100 |
| Days held | From your purchase settlement to now |
| YTM solver | How the yield was solved (for the curious; see the [method](METHOD.md)) |

### The cash-flow schedule

Every remaining payment is listed with:

- how many coupon periods away it is;
- its discount factor at the yield to maturity;
- its present value.

The present values add up to the dirty price. That's the definition of yield to maturity, and a quick way to check the result yourself.

## 5. The status badge

The badge at the top right tells you how current the figures are.

| Badge | Meaning | What to do |
|---|---|---|
| `OK` | The price is current. For Treasury prices that means the previous business day's or later | Nothing |
| `STALE` | The price is older than the previous business day | Usually nothing: it clears when the next prices are published. See the FAQ |
| `REUSED` | The latest refresh failed, so the last good price is shown again | Nothing; it retries on the next refresh |
| `NO DATA` | No price is available, for example an unknown CUSIP or an empty manual price | Check the CUSIP or enter a price |
| `ERROR` | An input is invalid; the message says which | Correct the field |

The line next to the badge shows the price date and when the prices were last downloaded.

## 6. Refreshing

The page refreshes itself every **60 seconds**. You can change this under **Refresh**, and **Refresh now** forces an update. Any edit to the form also recalculates straight away.

Treasury prices change once per business day. The regular refresh mainly keeps the settlement date and accrued interest current, and picks up new prices as soon as they're published.

## 7. Try the worked example

**Load worked example** fills in a 6% semi-annual bond maturing 15 June 2029, priced at 98.50 and bought at 97.00. It should show:

- yield to maturity **6.6164%**;
- current yield **6.0914%**;
- holding-period return **8.0711%**.

The [method](METHOD.md#8-worked-example-by-hand) works the same bond through by hand.

---

## Frequently asked questions

**Why aren't the prices live?**
Free, official bond prices are published once a day. Live intraday bond quotes come from paid terminals such as Bloomberg or Refinitiv. The code includes connectors for those as templates; see the [developer guide](DEVELOPER.md).

**When are new prices published?**
The site collects them on weekdays at 18:47 and 22:47 UTC, which is early afternoon and early evening in New York.

**Why does the badge say STALE the morning after a holiday?**
The page knows about weekends but not public holidays. After a holiday it expects prices for the holiday, which don't exist. It clears once the next business day's prices are published.

**Is my data stored anywhere?**
Only in your own browser, so your inputs are still there next time. Nothing is sent to a server. Clearing the site's data in your browser resets the page.

**Why is my broker's yield slightly different?**
The usual reasons are:

- a different settlement date;
- a different price (a bid or ask quote rather than the end-of-day price);
- a different day count;
- a "true yield" that shifts payments around holidays.

Gaps of a few hundredths of a percent are normal.

**Can I use it for Treasury bills, TIPS, floating-rate or callable bonds?**
No. Those follow different conventions. The page handles fixed-coupon bonds that repay at maturity.

**Is this investment advice?**
No. The figures are for information only.
