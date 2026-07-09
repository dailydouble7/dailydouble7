# Options Flow Predictor — a ConvexValue / cvforge app

A live, browser-based options-flow forecasting tool for
[ConvexValue](https://cvforge.convexvalue.com), ported from the
[Options-Flow-Predictor](https://github.com/NavnoorBawa/Options-Flow-Predictor)
machine-learning notebook.

It reads institutional "smart money" positioning from **live** ConvexValue
data and forecasts the short-term directional bias of the underlying at the
**1-, 3-, and 5-day** horizons — with a full, inspectable breakdown of every
signal that drove the call.

![app: prediction cards + signal breakdown + intraday flow chart]

## Run it

```bash
npm install
cvx login          # ConvexValue upload credentials
npm run dev        # local UI preview (data needs the ConvexValue origin)
npm run build      # bundle to dist/
npm run upload     # upload dist/ as the "options-flow-predictor" app
```

Launch from the ConvexValue command bar with:

```
ofp SPY
ofp NVDA
```

(`ofp <SYMBOL>`; symbol is also editable in the header. Defaults to `SPY`.)

Live data (`getUnd`, `getTrmChain`, `getFlowchart`, WebSocket) is authenticated
by the ConvexValue app-session cookie once the app is uploaded and launched from
the ConvexValue origin — so full data loads there, not in bare `npm run dev`.

### Using your own API key (run it anywhere)

To run the dashboard **outside** the ConvexValue origin, open **Settings ⚙** in
the header and paste your ConvexValue **API key**. The key is stored only in
your browser (localStorage) and sent as an `Authorization: Bearer` token on REST
calls and a `?token=` param on the WebSocket — nothing is hardcoded in the
bundle. If you're serving the app off-origin, also set the **API base URL** /
**WebSocket base URL** under *Advanced*. Leave the key blank to fall back to
ConvexValue's built-in session auth. Under the hood this drives the
`@convexvalue/app` `configureRuntime({ token, apiBaseUrl, wsBaseUrl })` runtime.

## How the notebook maps onto live ConvexValue data

The original notebook engineers four families of "smart money" signals and feeds
them to an ensemble (Random Forest + XGBoost). A cvforge app is static
client-side code with no pickled model and no training set (ConvexValue serves
*live* data, not the notebook's labeled history), so this is a **transparent
re-implementation** of that ensemble's decision logic over the same signal
families — nothing is a black box.

| Notebook concept            | Live ConvexValue fields                                   | Where |
|-----------------------------|-----------------------------------------------------------|-------|
| Put/Call ratio sentiment    | `put_call_ratio`, `call_volume`, `put_volume`             | `features.ts` |
| Directional / net flow      | `deltas_buy`, `deltas_sell`, `flowratio`                  | `features.ts` |
| Unusual volume detection    | `getFlowchart.flownet` z-score, `option_volume`           | `features.ts` |
| Dealer positioning (GEX/DEX)| `gxoi`, `dxoi` (gamma/delta × open interest)              | `features.ts` |
| Volatility structure / skew | `front_volatility`, `back_volatility`, `getTrmChain`      | `features.ts` |

### The ensemble (`model.ts`)

Each feature reduces to a directional signal in `[-1, 1]` (+ = bullish) and
feeds one of two sub-models:

- **Flow momentum** (XGBoost analog) — trend-following: net delta flow, premium
  flow ratio, unusual-flow burst, sentiment.
- **Dealer positioning** (Random Forest analog) — regime / mean-reversion:
  dealer delta hedging lean, IV term slope, contrarian sentiment extremes.

They are blended with **horizon-dependent weights** — momentum dominates the
near term and decays with horizon; positioning/mean-reversion dominates further
out — and modulated by the **dealer-gamma regime** (long gamma dampens & pins,
short gamma amplifies). The blended score maps through a logistic to `P(up)`,
which yields direction, confidence, and an IV-scaled expected move per horizon.

## Project layout

```
src/
  data.ts       live ConvexValue fetchers (getUnd / getTrmChain / getFlowchart)
  features.ts   signal engineering — the four notebook signal families
  model.ts      the horizon-weighted ensemble
  flowchart.ts  intraday net-flow vs price canvas chart
  format.ts     safe numeric coercion + formatting
  index.tsx     Solid.js UI, command parsing, live WebSocket
scripts/
  model.test.ts offline sanity checks for features + model
```

## Test

```bash
npm test
```

Runs synthetic bullish / bearish / neutral / empty bundles through the exact
feature + model code path and asserts sane outputs (probabilities in range,
directions respond to inputs, no NaN).

## Notes

Not investment advice. Signals are derived from live options flow and dealer
positioning and can be wrong; use as one input among many.
