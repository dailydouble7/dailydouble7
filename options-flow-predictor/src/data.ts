// Data layer for the Options Flow Predictor ConvexValue app.
//
// Everything the model needs is pulled from live ConvexValue data helpers:
//   - getUnd        underlying-level aggregated options flow + dealer greeks-x-OI
//   - getTrmChain   implied-volatility term structure per expiration
//   - getFlowchart  intraday net-flow time series
//   - getUndPrice   spot lookup
//
// Responses are positional arrays for compact transport, so each fetch keeps a
// column list beside the rows and maps values by index. Missing/NaN values are
// normalized by `num()` at read time.

import { getUnd, getTrmChain, getFlowchart, getUndPrice } from "@convexvalue/app";
import { num } from "./format";

// ---- Underlying flow snapshot -------------------------------------------------

// Parameters requested from `und`. Order here defines the column layout used to
// decode each returned row ([symbol, ...UND_PARAMS]).
export const UND_PARAMS = [
    "price", "change", "put_call_ratio", "call_volume", "put_volume", "option_volume",
    "volatility", "front_volatility", "back_volatility",
    "flowratio", "vflowratio",
    "value", "volm",
    "value_buy", "value_sell",
    "value_call_buy", "value_put_buy", "value_call_sell", "value_put_sell",
    "deltas_buy", "deltas_sell",
    "deltas_call_buy", "deltas_put_buy", "deltas_call_sell", "deltas_put_sell",
    "gammas_buy", "gammas_sell",
    "gxoi", "dxoi", "vxoi", "charmxoi", "vannaxoi",
    "gxvolm", "dxvolm",
] as const;

export type UndSnapshot = Record<(typeof UND_PARAMS)[number], number> & { symbol: string };

interface GetUndRes {
    data?: unknown[];
}

export async function fetchUnd(symbol: string): Promise<UndSnapshot> {
    const params = [...UND_PARAMS];
    const res = await getUnd<GetUndRes>({
        symbols: [symbol],
        params,
        query: `select symbol from und where symbol='${symbol}' limit 1`,
    });

    // res.data is an array of rows; each row is [symbol, ...params]. Pick the
    // row for our symbol, falling back to the first row.
    const rows = Array.isArray(res?.data) ? (res.data as unknown[][]) : [];
    const row =
        rows.find((r) => Array.isArray(r) && String(r[0]).toUpperCase() === symbol) ??
        (Array.isArray(rows[0]) ? (rows[0] as unknown[]) : []);

    const snap = { symbol } as UndSnapshot;
    params.forEach((p, i) => {
        // row[0] is the symbol, so requested params begin at index 1.
        (snap as Record<string, number>)[p] = num(row[i + 1]);
    });
    return snap;
}

// ---- Term structure -----------------------------------------------------------

export const TRM_PARAMS = [
    "expiration", "volatility", "call_volume", "put_volume", "put_call_ratio", "option_volume",
] as const;

export interface TermRow {
    expiration: number;
    volatility: number;
    call_volume: number;
    put_volume: number;
    put_call_ratio: number;
    option_volume: number;
}

interface GetTrmChainRes {
    data?: { series?: unknown[]; spot?: number }[];
}

export async function fetchTerm(symbol: string): Promise<TermRow[]> {
    const params = [...TRM_PARAMS];
    const res = await getTrmChain<GetTrmChainRes>({ symbols: [symbol], params });
    const series = Array.isArray(res?.data?.[0]?.series) ? (res!.data![0]!.series as unknown[]) : [];

    return series
        .filter((r): r is unknown[] => Array.isArray(r))
        .map((r) => {
            const row = {} as Record<string, number>;
            params.forEach((p, i) => (row[p] = num(r[i])));
            return row as unknown as TermRow;
        })
        .sort((a, b) => a.expiration - b.expiration);
}

// ---- Intraday flow series -----------------------------------------------------

export const FLOW_COLS = ["price", "flownet", "vflownet", "value_call_bs", "value_put_bs"] as const;

export interface FlowPoint {
    t: number;
    price: number;
    flownet: number;
    vflownet: number;
    callBs: number;
    putBs: number;
}

interface GetFlowchartRes {
    data?: unknown[];
}

export async function fetchFlow(symbol: string): Promise<FlowPoint[]> {
    const day = Math.floor(Date.now() / 864e5);
    const cols = [...FLOW_COLS];
    const res = await getFlowchart<GetFlowchartRes>({ symbol, cols, day });

    const headers = Array.isArray(res?.data?.[0]) ? (res!.data![0] as unknown[]).map(String) : [];
    const rows = Array.isArray(res?.data?.[1]) ? (res!.data![1] as unknown[][]) : [];

    // Map by header name so we are robust to leading day/time columns.
    const idx = (name: string) => headers.indexOf(name);
    const iTime = headers.indexOf("time") >= 0 ? headers.indexOf("time") : 1;
    const iPrice = idx("price");
    const iFlow = idx("flownet");
    const iVFlow = idx("vflownet");
    const iCall = idx("value_call_bs");
    const iPut = idx("value_put_bs");

    return rows
        .filter((r): r is unknown[] => Array.isArray(r))
        .map((r) => ({
            t: num(r[iTime]),
            price: num(r[iPrice]),
            flownet: num(r[iFlow]),
            vflownet: num(r[iVFlow]),
            callBs: num(r[iCall]),
            putBs: num(r[iPut]),
        }))
        .sort((a, b) => a.t - b.t);
}

// ---- Combined bundle ----------------------------------------------------------

export interface MarketBundle {
    symbol: string;
    spot: number;
    und: UndSnapshot;
    term: TermRow[];
    flow: FlowPoint[];
}

export async function fetchBundle(symbol: string): Promise<MarketBundle> {
    const sym = symbol.toUpperCase();
    // Fetch in parallel; tolerate partial failures on the non-critical series so
    // a single flaky endpoint does not blank the whole prediction.
    const [und, term, flow, spot] = await Promise.all([
        fetchUnd(sym),
        fetchTerm(sym).catch(() => [] as TermRow[]),
        fetchFlow(sym).catch(() => [] as FlowPoint[]),
        getUndPrice(sym).catch(() => 0),
    ]);
    return {
        symbol: sym,
        spot: num(spot) || und.price,
        und,
        term,
        flow,
    };
}
