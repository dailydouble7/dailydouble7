# Options Flow Predictor — build spec

A ConvexValue (cvforge) app that ports the
[Options-Flow-Predictor](https://github.com/NavnoorBawa/Options-Flow-Predictor)
machine-learning notebook into a live, browser-based market tool.

## Goal

Given a symbol, forecast the short-term directional bias (1-, 3-, and 5-day) of
the underlying by reading institutional "smart money" options positioning from
live ConvexValue data, and show *why* — every signal and its contribution is
inspectable.

## First screen

Dense, single-screen dashboard (no landing page):

1. **Spot bar** — symbol, live price (WebSocket), change, PCR, front IV, option volume.
2. **Prediction cards** — one per horizon (1D / 3D / 5D): direction, P(up),
   expected move + price target, confidence meter.
3. **Ensemble internals** — the two sub-model scores (flow-momentum &
   dealer-positioning), the dealer-gamma regime, and flow intensity.
4. **Signal breakdown** — every engineered feature with its raw value,
   normalized signal, sub-model, and signed contribution bar.
5. **Intraday net flow vs price** — canvas chart (green/red net-flow area +
   price line), DPR-aware, redraws on resize.
6. **IV term structure** — per-expiration IV / PCR / option volume.

## Data sources (live ConvexValue helpers)

- `getUnd` — underlying flow aggregates + dealer greek-×-OI (`gxoi`, `dxoi`, …).
- `getTrmChain` — IV term structure per expiration.
- `getFlowchart` — intraday net-flow series (`flownet`, `vflownet`, …).
- `getUndPrice` + `createWebSocket("/api/ws")` — spot + live updates.

## Command grammar

`ofp <SYMBOL>` — e.g. `ofp SPY`, `ofp NVDA`. Symbol also editable in the header.
Defaults to `SPY` when omitted.

## Constraints

- Static client-side only. No backend, no secrets, no hardcoded credentials.
- Treat all API rows as untrusted: guard indexes, coerce with `Number()`.
- Always render loading / empty / error states.
