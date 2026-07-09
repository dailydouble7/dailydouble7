# ConvexValue App Agent Guide

This project is a static browser app scaffolded for ConvexValue as `options-flow-predictor` (`Options Flow Predictor`). The purpose of this app is to be built by a coding agent, bundled into `dist/`, and uploaded to ConvexValue so users can run it from the ConvexValue command bar.

## What To Build

Build the actual market-data tool as the first screen. Do not build a marketing landing page. ConvexValue users expect dense, fast, inspectable option and market data views with useful loading, empty, and error states.

The app must be static client-side code. Do not add a backend service, do not require server-side secrets, and do not hardcode user credentials, cookies, API tokens, or private URLs. ConvexValue serves uploaded apps from `https://username--app.convexvalue.app` and authenticates app data APIs with a host-only app session cookie.

## Project Commands

- `npm install`: install dependencies.
- `npm run dev`: run the local Vite server for UI-only preview.
- `npm run build`: build the static app into `dist/`.
- `cvx login`: save ConvexValue upload credentials locally.
- `npm run upload`: build artifact upload command; it uploads `dist/` as app `options-flow-predictor`.

Use `npm run dev` for local UI iteration. Use `npm run build` and `npm run upload` to test authenticated data access from the ConvexValue app origin.

## App Contract

Import command and data helpers from `@convexvalue/app`:

```js
import {
  getCommand,
  getCommandRoot,
  getChain,
  getChainH,
  getDeltas,
  getEarnCal,
  getEconCal,
  getFlowchart,
  getMatrix,
  getOpt,
  getTas,
  getTrmChain,
  getTrmH,
  getUnd,
  getUndParams,
  getUndPrice,
  createWebSocket,
} from "@convexvalue/app";
```

`getCommand()` reads the `q` query parameter that ConvexValue passes when launching the app. Example: if the command bar launches `options-flow-predictor AAPL value exp=1-5`, then `getCommand()` returns that whole string. `getCommandRoot(command)` returns the first whitespace-delimited token. Treat the root as the launch token and parse the remaining tokens as app arguments.

Data helpers call same-origin `/api/get`, `/api/data`, and `/api/ws`. The browser sends the app session cookie automatically when the uploaded app is launched from ConvexValue. Do not manually read cookies.

The uploaded app is served as an isolated static app. Only files in `dist/` are uploaded. The bundle must include a root `index.html`.

## Command Parsing Pattern

Use a simple command grammar that is easy for users and agents to modify:

- The first token is usually the app launch token.
- The next bare token is often a symbol/root, for example `AAPL`, `SPY`, or `SPX`.
- `key=value` tokens configure the app, for example `cols=value,price`, `orderby=value`, `filters=value>1000000`, `exp=1-5`, `rng=10`.
- Comma-separated values should be uppercased for symbols and kept as exact parameter names for data columns.
- Support sensible defaults when arguments are omitted.

Example parser sketch:

```js
const raw = getCommand() || "options-flow-predictor SPY";
const [, ...tokens] = raw.trim().split(/\s+/);
const options = {};
const positional = [];

for (const token of tokens) {
  const eq = token.indexOf("=");
  if (eq === -1) positional.push(token);
  else options[token.slice(0, eq)] = token.slice(eq + 1);
}

const symbol = (positional[0] || "SPY").toUpperCase();
const cols = (options.cols || "value,price,change").split(",");
```

## Data API Reference

All helpers return the JSON response from ConvexValue. Most response data is positional arrays for compact transport, so always keep a column list next to the rows and normalize missing/null/NaN values before rendering.

### Chain Data

Use `getChain` for option chains by root symbol:

```js
const res = await getChain({
  symbols: ["AAPL"],
  params: ["value", "delta"],
  exps: [0, 1, 2],
  rng: 0.10,
  series: false,
});
```

Request shape: `{ symbols: string[], params: string[], exps?: number[], rng?: number, series?: boolean }`.

Common response shape:

```js
{
  data: [{
    spot: 226.59,
    chain: [
      [19916, [
        [220, [".AAPL240712C220", 21328, ...params], [".AAPL240712P220", 16608, ...params]]
      ]]
    ]
  }]
}
```

Each chain item is `[expirationDayId, strikes]`. Each strike item is `[strike, callArray, putArray]`. `callArray[0]` and `putArray[0]` are option symbols. Requested param values start at index `1` in the same order as `params`.

Use `getChainH({ root, params, exps, range, day_id })` for historical chain rows. Common response shape is `data: [headers, rows]`.

### Underlying Scanner Data

Use `getUnd` for underlying-level scans and watchlists:

```js
const params = ["value", "price", "change", "volm_call_buy", "volm_put_buy"];
const res = await getUnd({
  params,
  query: "select symbol from und where value>1000000 order by value desc nulls last limit 50",
  symbols: ["AAPL", "MSFT"],
});
const rows = res.data?.[0] || [];
```

Request shape: `{ symbols?: string[], params: string[], query: string }`.

Rows are usually `[symbol, ...params]`. Build a `columns = ["symbol", ...params]` array and map by index.

### Option Scanner Data

Use `getOpt` for option-contract scans:

```js
const params = ["symbol", "price", "value", "delta", "expiration", "strike"];
const res = await getOpt({
  params,
  query: "select symbol from opt where value>100000 order by value desc nulls last limit 100",
});
```

Request shape: `{ symbols?: string[], params: string[], query: string }`.

Prefer controlled query builders over concatenating arbitrary user input. Keep user-configurable filters limited to known columns and operators.

### Time And Sales

Use `getTas` for trade tape/table views:

```js
const res = await getTas({
  cols: ["time", "symbol", "price", "value", "size", "delta"],
  orderby: "value",
  limit: 200,
  asc: false,
  futs: false,
  s: [],
  filters: [{ Gt: ["delta", 0] }, { Gt: ["size", 100] }],
  day: 0,
  roots: ["NVDA", "AAPL"],
  symbols: undefined,
  like: undefined,
  side: "buy",
});
const [headers, rows] = res.data?.[0] ? res.data : [[], []];
```

Supported filter object forms are `{ Gt: [column, number] }`, `{ Lt: [column, number] }`, and `{ Eq: [column, number] }`. Useful command syntax mirrors the built-in TAS app: `tas orderby=value roots=nvda,aapl filters=delta<0.1,delta>0,size>100 cols=time,symbol,price,value,size`.

### Flowchart Data

Use `getFlowchart` for intraday time series by root:

```js
const day = Math.floor(Date.now() / 864e5);
const res = await getFlowchart({
  symbol: "SPY",
  cols: ["price", "flowratio", "prop1", "prop4", "deltas"],
  day,
});
const headers = res.data?.[0] || [];
const rows = res.data?.[1] || [];
```

Rows are typically sorted by the time column at index `1`. Data columns start after the first two columns.

### Term Structure

Use `getTrmChain` and `getTrmH` for implied-volatility term structure:

```js
const chain = await getTrmChain({
  symbols: ["AAPL"],
  params: ["expiration", "volatility", "call_volume", "put_volume", "put_call_ratio", "forward_price", "option_volume", "dividend", "interest"],
});

const history = await getTrmH({
  root: "AAPL",
  expirations: [0, 1, 2],
  start_day: 5,
});
```

`getTrmChain` commonly returns `data[0].series` and `data[0].spot`. `getTrmH` commonly returns `data: [headers, rows]`.

### Direct Underlying Values

Use `getUndParams` or `getUndPrice` for small point lookups:

```js
const price = await getUndPrice("SPY");
const res = await getUndParams({ s: ["SPY", "QQQ"], v: ["price", "change"] });
```

### Calendars And Other Helpers

`getEarnCal(days)` returns earnings calendar data. `getEconCal(days)` returns economic calendar data. `getMatrix({ symbol })`, `getDeltas({ symbol })`, and lower-level `wrapGet`/`wrapPost` are available for specialized apps.

## WebSocket Pattern

Use `createWebSocket()` for same-origin app data streams:

```js
const socket = createWebSocket("/api/ws");

socket.addEventListener("open", () => {
  socket.send(JSON.stringify([{ On: { s: ["SPY"], v: ["price", "change"] } }]));
});

socket.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  // Updates commonly include a symbol/root and an array of value objects.
  // Existing apps handle shapes like: { s: "SPY", v: [{ price: 500.12 }] }.
});

function resubscribe(symbols, values) {
  socket.send(JSON.stringify(["Clear"]));
  socket.send(JSON.stringify([{ On: { s: symbols, v: values } }]));
}
```

Close sockets and remove listeners when replacing views or unmounting components.

## Parameter Lists

Use these exact parameter identifiers in API requests.

Options parameters:

```txt
opt_kind, expiration, multiplier, product, mmy, last_trade, strike, g_time, theo, volatility, delta, gamma, theta, rho, vega, event_time, day_id, day_open_price, day_high_price, day_low_price, day_close_price, day_close_price_type, prev_day_id, prev_day_close_price, prev_day_close_price_type, prev_day_volume, oi, oi_ch, bid_time, bid_exchange_code, bid_price, bid_size, ask_time, ask_exchange_code, ask_price, ask_size, t_time, exchange_code, price, change, size, t_day_id, day_volume, day_turnover, tick_direction, spread, value, volm, deltas, gammas, vegas, thetas, rhos, value_buy, volm_buy, deltas_buy, gammas_buy, vegas_buy, thetas_buy, rhos_buy, value_sell, volm_sell, deltas_sell, gammas_sell, vegas_sell, thetas_sell, rhos_sell, value_und, volm_und, deltas_und, gammas_und, vegas_und, thetas_und, rhos_und, value_bs, volm_bs, expiration_ts, vanna, vomma, charm, dxoi, gxoi, vxoi, txoi, vannaxoi, vommaxoi, charmxoi, gxvolm, vxvolm, txvolm, vannaxvolm, vommaxvolm, charmxvolm, dxvolm, volm_5m, value_5m, volmbs_5m, valuebs_5m, volm_15m, value_15m, volmbs_15m, valuebs_15m, volm_30m, value_30m, volmbs_30m, valuebs_30m, volm_60m, value_60m, volmbs_60m, valuebs_60m
```

Underlying parameters:

```txt
event_time, day_id, day_open_price, day_high_price, day_low_price, day_close_price, day_close_price_type, prev_day_id, prev_day_close_price, prev_day_close_price_type, prev_day_volume, oi, t_time, exchange_code, price, change, size, t_day_id, day_volume, day_turnover, tick_direction, bid_time, bid_exchange_code, bid_price, bid_size, ask_time, ask_exchange_code, ask_price, ask_size, spread, u_time, volatility, front_volatility, back_volatility, call_volume, put_volume, put_call_ratio, option_volume, high_52_week_price, low_52_week_price, value, volm, deltas, gammas, vegas, thetas, rhos, value_buy, volm_buy, deltas_buy, gammas_buy, vegas_buy, thetas_buy, rhos_buy, value_call_buy, volm_call_buy, deltas_call_buy, gammas_call_buy, vegas_call_buy, thetas_call_buy, rhos_call_buy, value_put_buy, volm_put_buy, deltas_put_buy, gammas_put_buy, vegas_put_buy, thetas_put_buy, rhos_put_buy, value_sell, volm_sell, deltas_sell, gammas_sell, vegas_sell, thetas_sell, rhos_sell, value_call_sell, volm_call_sell, deltas_call_sell, gammas_call_sell, vegas_call_sell, thetas_call_sell, rhos_call_sell, value_put_sell, volm_put_sell, deltas_put_sell, gammas_put_sell, vegas_put_sell, thetas_put_sell, rhos_put_sell, value_und, volm_und, deltas_und, gammas_und, vegas_und, thetas_und, rhos_und, value_call_und, volm_call_und, deltas_call_und, gammas_call_und, vegas_call_und, thetas_call_und, rhos_call_und, value_put_und, volm_put_und, deltas_put_und, gammas_put_und, vegas_put_und, thetas_put_und, rhos_put_und, value_bs, volm_bs, flowratio, vflowratio, value_call_ratio, value_put_ratio, volm_call_ratio, volm_put_ratio, dxoi, gxoi, vxoi, txoi, vannaxoi, charmxoi, gxvolm, vxvolm, txvolm, vannaxvolm, charmxvolm, dxvolm
```

Time and Sales columns:

```txt
symbol, event_flags, index, time, sequence, exchange_code, price, size, bid_price, ask_price, exchange_sale_conditions, trade_through_exempt, aggressor_side, spread_leg, extended_trading_hours, valid_tick, tas_type, value, spot, gamma, delta, vega, theta, rho, volatility, theo
```

Flowchart parameters:

```txt
call_dxoi, call_gxoi, call_vxoi, call_txoi, put_dxoi, put_gxoi, put_vxoi, put_txoi, value_call_bs, value_put_bs, volm_call_bs, volm_put_bs, flownet, vflownet, prop1, prop2, prop3, prop4
```

## Built-In App Patterns To Reuse

- Flow scanner: `getUnd({ query, params, symbols })`; command examples include `flow cols=value,price,change,volm_call_buy orderby=volm_call_buy filters=value>1000000`.
- TAS tape: `getTas({ cols, orderby, limit, filters, roots, symbols, side, day })`; render `[headers, rows]`.
- Joy ridgeline: `getChain({ symbols: [root], params: [param], exps, rng })`; render expiration stacks from `data[0].chain` and subscribe to option symbols plus spot over WebSocket.
- Flowchart: `getFlowchart({ symbol, cols, day })`; render time series from `data[1]`.
- Terms: `getTrmChain` for current term structure and `getTrmH` for historical series.

## Implementation Standards

- Keep UI state deterministic from command input and API responses.
- Always show loading, empty, and error states.
- Preserve user-provided command options in the URL/query when appropriate.
- Normalize symbols to uppercase, but do not uppercase parameter names.
- Treat API rows as untrusted external data; guard indexes and convert numbers with `Number(value)`.
- For charts, use stable dimensions, `ResizeObserver`, and redraw on device-pixel-ratio changes.
- For high-frequency updates, separate static chart layers from dynamic overlays or batch updates with `requestAnimationFrame`.
- Do not block interactions while streaming updates arrive.
- Keep accessibility basics: readable contrast, keyboard-reachable controls, and text alternatives for non-obvious controls.

## Upload Checklist

Before upload:

1. Run `npm run build`.
2. Confirm `dist/index.html` exists.
3. Confirm no secrets or local-only URLs are in the bundle.
4. Run `npm run upload`.
5. Test from ConvexValue after upload to confirm the isolated app origin and data access.
