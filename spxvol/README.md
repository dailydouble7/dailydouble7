# spxvol

Estimate implied volatility at the moment of a trade from **option OHLCV bars
alone**, and flag large SPX prints that paid above the prevailing market for
their delta.

Built against the cvforge MCP data service (`get_option_bars`), which serves
Polygon-shaped aggregates carrying `vwap` and `transactions` alongside OHLCV. A
cvforge key is enough — **no paid ConvexValue subscription is required**, and no
index price feed is needed either.

Pure standard library. No numpy, no pandas, no `requests`.

```bash
python3 -m spxvol.cli selftest     # validates the whole chain offline, no API key
python3 -m unittest discover -s tests
```

---

## Why bars are enough

Two facts make trade-level work possible without a tape.

**1. `transactions` identifies individual prints.** When a minute bar has
`transactions == 1`, the bar *is* a single trade: open, high, low and close all
equal the traded price, and `volume` is the exact contract count. More generally
`volume / transactions` separates block activity (few, huge prints) from retail
churn (many, small ones). `Bar.print_quality` classifies this as `exact`,
`block`, `mixed` or `retail`.

**2. Put-call parity recovers the forward.** You cannot get IV from a price
alone — you need the forward and the discount factor. For a European,
cash-settled index option, parity is exact and *linear in the strike*:

```
C - P = D * (F - K)
```

Regress `C - P` on `K` across several strikes at one timestamp: the slope is
`-D` and the intercept is `D * F`. Both unknowns fall out at once.

This is the single most important design decision here. The alternative —
assuming a rate curve and a dividend forecast — puts a systematic tilt across
the entire SPX smile, which is precisely the quantity a skew model is trying to
measure. Parity needs no rate, no dividend yield, and **no index level**. It
also self-corrects for clock drift, since both legs come from the same minute.

Delta then costs nothing extra: IV comes from price, and delta comes from the
same `d1`. The apparent circularity (delta depends on vol, vol depends on
price) resolves itself.

### Do you need an intraday SPX price?

No. But if you want spot-delta conventions or an implied dividend yield, run:

```bash
python3 -m spxvol.cli probe
```

This asks *your key* what it can actually serve — scanning the MCP tool list and
searching the FMP endpoint catalogue for chart/quote/index endpoints — rather
than relying on documentation, since entitlements vary by plan. Given a spot,
`black76.dividend_discount()` recovers `exp(-qT)` and `black76.spot_delta()`
switches conventions. Everything else works without it.

---

## The two-layer model

"Paid above historical pricing for that delta" has a failure mode worth
designing around: **without controlling for the prevailing vol level, the model
degenerates into a VIX detector** and flags every trade on a high-vol day. So
the measurement is split in two, reported separately.

### Layer 1 — aggression

*Was this print above the market at that instant?*

Fit a smile to the same minute, then measure the trade's IV as a residual
against that curve. Timestamp-local, so it is immune to the vol level entirely.
This is the cleaner and more robust of the two signals.

The print's own contract is **excluded from the fit that judges it**
(`fit_smile(..., exclude_tags=...)`). Without that leave-one-out step a large
print drags the curve toward its own price and its richness disappears.

Reported as `residual_vol_pts`, and as `dollars_over_curve` —
`(iv_trade - iv_fit) × vega × contracts × 100` — which is the more intuitive
number: how many dollars over the curve they paid.

### Layer 2 — richness

*Was that delta expensive versus its own history?*

Take the fitted smile at a target delta and normalise it as a spread to ATM vol,
then rank it against its own trailing distribution, bucketed by **both** delta
and tenor (a 25Δ put at 7 DTE and at 90 DTE are different instruments).
Normalising is what makes this a skew signal rather than a level signal.

Reported as `skew_percentile` with its `skew_sample` size.

A trade high on both layers is a buyer paying up for something already
expensive.

---

## Usage

```bash
export CVFORGE_API_KEY=...        # or CV_API_KEY, or the *_FILE variants

# What can this key reach?
python3 -m spxvol.cli tools
python3 -m spxvol.cli probe

# Build the Layer-2 trailing distribution (uses 1d bars: one call per
# contract covers the whole window, so a year costs about as much as one
# intraday session).
python3 -m spxvol.cli history \
  --root SPXW --expiries 2026-07-31,2026-08-31 \
  --center 6000 --start 2025-08-01 --end 2026-06-30 --out skew.json

# Scan a session for large prints that paid up.
python3 -m spxvol.cli scan \
  --root SPXW --expiry 2026-07-31 --session 2026-07-02 \
  --center 6000 --band 0.05 --step 25 \
  --min-volume 250 --history skew.json
```

Output columns: trade time, contract, size, print quality, price, IV residual in
vol points, dollars over the curve, skew percentile, composite score.

Library use:

```python
from spxvol import solve_forward, implied_vol, fit_smile, ParityQuote

solution = solve_forward([ParityQuote(k, call_px[k], put_px[k]) for k in strikes])
iv = implied_vol(trade_price, solution.forward, strike, tenor, is_call, solution.discount)
```

### Cost control

The SPX chain is enormous and it is one API call per contract (5000-row cap).
A ±5% band at 25-point spacing is ~50 contracts, so one session of 1-minute bars
is ~50 calls. Bars are cached gzipped under `~/.cache/spxvol` keyed by contract
and date range, so re-scans are free. Use `--step 5` only once a candidate
window is identified.

---

## Details that bite

**Tenor is measured in trading time, not calendar days.** Calendar-day `T` makes
0DTE IV explode into the close, contaminating exactly the short-dated trades
this is built to measure. `trading_calendar.py` implements the NYSE session
calendar (including Good Friday via the Gregorian Easter algorithm, the
Saturday-holiday non-observance rule, and 1pm early closes) and counts real
trading minutes.

Note the two clocks do not order consistently: July 2026 packs 23 sessions into
31 days, so trading time runs *faster* than ACT/365 that month. Only trading
time is used internally.

**SPX monthlies are AM-settled; SPXW is PM-settled.** Same-looking expiry date,
materially different tenor intraday — SPX settles from Friday's *opening* prints,
so its time value ends at 09:30, not 16:00. Getting this wrong produces a fake
term-structure kink. Handled by `OptionContract.is_am_settled`.

**Bar close is a traded price, not a mid.** For scoring trades that is a feature
— you want the traded price. But it means the IV series has bid-ask crossing
baked in, so the parity fit and the smile deliberately consume `vwap` (a
mid-like central estimate) while the trade scoring consumes `close` (the print).
Never compare trade-derived IV to a mid-derived surface.

**Uninvertible prices are rejected, not clamped.** Prices at or beyond the
no-arbitrage bounds (usually a stale or mismatched-timestamp leg) return a
reason rather than a number. So do prices whose *time value* is negligible: once
an option is worth ~1e-12 its price no longer depends on vol at all, and an
absolute price tolerance would otherwise "converge" on the bracket floor and
emit a fake near-zero vol point.

---

## What this cannot do

Honest limits, all of them data limitations rather than modelling ones:

- **Trade direction is inferred, not known.** A print above the fitted curve is
  consistent with a buyer lifting the offer, below with a seller hitting the
  bid. `direction_hint` reports this as a proxy. Bars carry no side flag.
- **Spread legs look like outright trades.** A large put printing "aggressively"
  may be one leg of a risk reversal or a hedge against stock, not a directional
  bet. This is the main source of false positives.
- **Multi-print minutes blur.** Only `transactions == 1` bars give an exact
  trade price; everything else is a size-weighted approximation.
- **Layer-2 percentiles are ordinal, not calibrated.** The history is
  reconstructed from traded prices rather than quotes, so treat the ranking as
  an ordering, not a probability.
- **Composite score weights are a starting point**, not a fitted model. The
  components (`aggression_z`, size, `skew_percentile`) are all reported
  separately so you can re-weight or ignore the composite.

The paid ConvexValue `tas` endpoint would resolve the first three directly.

---

## Layout

| Module | Role |
| --- | --- |
| `black76.py` | Pricing, safeguarded-Newton IV inversion, greeks, delta↔strike |
| `forward.py` | Put-call parity regression → `(F, D)` with diagnostics |
| `trading_calendar.py` | NYSE sessions, holidays, early closes, trading minutes |
| `tenor.py` | Contract tenor, AM/PM settlement |
| `occ.py` | OCC symbol encode/decode |
| `bars.py` | Bar records, print-quality classification |
| `smile.py` | Vega-weighted total-variance fit, delta buckets, leave-one-out |
| `signal.py` | Two-layer scoring, trailing skew history |
| `pipeline.py` | Strike grids, minute alignment, session scan, history build |
| `source.py` | cvforge MCP client (stdlib only), caching, chunking, probing |
| `cli.py` | `selftest`, `tools`, `probe`, `scan`, `history` |

`selftest` builds a surface with a known forward, discount and skew, prices a
chain from it, and checks that parity recovers `(F, D)` to ~1e-13, that
inversion recovers the input vols to ~1e-13, and that a print made rich by
exactly 2 vol points is measured at +2.000.

## Attribution

Inspired by the endpoint surface of the unofficial
[`tradeanon/convexvalue-client`](https://github.com/tradeanon/convexvalue-client)
(MIT). No code is copied from it; the MCP transport here is an independent
stdlib implementation. Not affiliated with ConvexValue.
