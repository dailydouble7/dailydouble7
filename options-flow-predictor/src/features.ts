// Feature engineering — the port of the Options-Flow-Predictor notebook's
// signal families onto live ConvexValue data.
//
// The original notebook engineers four families of "smart money" signals:
//   1. Put/Call ratio (sentiment)
//   2. Unusual volume / flow aggression
//   3. Dealer positioning (gamma / delta exposure)
//   4. Volatility structure (term slope / skew)
//
// Each feature below reduces raw ConvexValue fields to a single directional
// signal in [-1, 1] (+ = bullish for the underlying), tagged with the sub-model
// it feeds and a base weight. This keeps the whole model inspectable: the UI can
// show exactly how each field pushed the prediction.

import type { MarketBundle } from "./data";
import { clamp, num } from "./format";

export type SubModel = "momentum" | "positioning";

export interface Feature {
    key: string;
    label: string;
    model: SubModel;
    weight: number; // base weight within its sub-model
    signal: number; // directional, [-1, 1], + = bullish
    raw: string; // human-readable raw value for the breakdown table
    note: string; // one-line interpretation
}

export interface FeatureSet {
    features: Feature[];
    // Regime / magnitude modifiers derived from dealer gamma and flow intensity.
    gammaRegime: number; // -1 (short gamma, amplifies) .. +1 (long gamma, dampens)
    flowIntensity: number; // 0..1 conviction booster from unusual flow
    dailySigma: number; // expected 1-day move as a fraction of spot
}

// z-score of the last value of a series against the series itself.
function lastZ(series: number[]): number {
    if (series.length < 3) return 0;
    const mean = series.reduce((a, b) => a + b, 0) / series.length;
    const variance =
        series.reduce((a, b) => a + (b - mean) * (b - mean), 0) / series.length;
    const sd = Math.sqrt(variance);
    if (sd === 0) return 0;
    return (series[series.length - 1] - mean) / sd;
}

export function buildFeatures(bundle: MarketBundle): FeatureSet {
    const u = bundle.und;
    const features: Feature[] = [];

    // --- 1. Put/Call ratio sentiment ------------------------------------------
    // Call-heavy tape (PCR < ~0.9) leans bullish; put-heavy leans bearish. Used
    // as a momentum/sentiment feature.
    const pcr = u.put_call_ratio || safePcr(u.put_volume, u.call_volume);
    const pcrSignal = clamp((0.9 - pcr) / 0.6);
    features.push({
        key: "pcr",
        label: "Put/Call ratio",
        model: "momentum",
        weight: 0.18,
        signal: pcrSignal,
        raw: pcr ? pcr.toFixed(2) : "—",
        note: pcr < 0.9 ? "Call-heavy tape (bullish)" : "Put-heavy tape (bearish)",
    });

    // --- 2. Net directional delta flow ----------------------------------------
    // Signed delta initiated by aggressive buyers vs sellers. The cleanest read
    // on which way smart money is positioning right now.
    const netDelta = u.deltas_buy - u.deltas_sell;
    const deltaBase = Math.abs(u.deltas_buy) + Math.abs(u.deltas_sell) || 1;
    const deltaSignal = clamp(netDelta / deltaBase);
    features.push({
        key: "net_delta",
        label: "Net delta flow",
        model: "momentum",
        weight: 0.40,
        signal: deltaSignal,
        raw: fmtSigned(netDelta),
        note: netDelta >= 0 ? "Buyers lifting delta (bullish)" : "Sellers pressing delta (bearish)",
    });

    // --- 3. Premium flow ratio -------------------------------------------------
    // flowratio is the net buy-vs-sell premium tilt. Momentum feature.
    const flowSignal = clamp(normalizeRatio(u.flowratio));
    features.push({
        key: "flow_ratio",
        label: "Premium flow ratio",
        model: "momentum",
        weight: 0.24,
        signal: flowSignal,
        raw: u.flowratio ? u.flowratio.toFixed(2) : "—",
        note: flowSignal >= 0 ? "Net premium bought (bullish)" : "Net premium sold (bearish)",
    });

    // --- 4. Unusual intraday flow (directional component) ---------------------
    // Direction comes from the sign of the latest intraday net flow; magnitude
    // (its z-score) is folded into flowIntensity below to scale conviction.
    const flowSeries = bundle.flow.map((p) => p.flownet);
    const flowZ = lastZ(flowSeries);
    const unusualSignal = clamp(flowZ / 2.5);
    features.push({
        key: "unusual_flow",
        label: "Unusual flow (intraday z)",
        model: "momentum",
        weight: 0.18,
        signal: unusualSignal,
        raw: flowSeries.length ? `${flowZ.toFixed(1)}σ` : "—",
        note: Math.abs(flowZ) > 1.5 ? "Abnormal flow burst" : "Flow within normal range",
    });

    // --- 5. Dealer delta hedging lean -----------------------------------------
    // dxoi is dealer delta exposure. When dealers are net short delta they must
    // buy the underlying to stay hedged — a supportive (bullish) bias, and the
    // reverse when they are long. Positioning feature.
    const dxoiSignal = clamp(-Math.sign(u.dxoi) * Math.min(1, Math.abs(u.dxoi) / dealerNorm(u.dxoi)));
    features.push({
        key: "dealer_delta",
        label: "Dealer delta hedging",
        model: "positioning",
        weight: 0.32,
        signal: dxoiSignal,
        raw: fmtSigned(u.dxoi),
        note: u.dxoi < 0 ? "Dealers buy to hedge (support)" : "Dealers sell to hedge (resistance)",
    });

    // --- 6. Volatility term slope / skew --------------------------------------
    // Normal term structure (back IV >= front IV) is calm/constructive; an
    // inverted front (fear bid in near-dated vol) is bearish. Positioning.
    const front = u.front_volatility || firstTermVol(bundle);
    const back = u.back_volatility || lastTermVol(bundle);
    const slope = front > 0 ? (back - front) / front : 0;
    const skewSignal = clamp(slope * 2.5);
    features.push({
        key: "term_slope",
        label: "IV term slope",
        model: "positioning",
        weight: 0.30,
        signal: skewSignal,
        raw: front ? `${(front * 100).toFixed(0)}→${(back * 100).toFixed(0)}` : "—",
        note: slope >= 0 ? "Contango / calm term" : "Inverted / near-term fear",
    });

    // --- 7. Contrarian sentiment extreme --------------------------------------
    // At extremes, sentiment mean-reverts: very put-heavy tape often marks a
    // bottom. This positioning feature deliberately opposes feature #1 at the
    // tails, mirroring the notebook's mean-reversion ensemble member.
    const extreme = pcr > 1.3 ? 1 : pcr < 0.55 ? -1 : 0;
    features.push({
        key: "pcr_contrarian",
        label: "Sentiment extreme (contrarian)",
        model: "positioning",
        weight: 0.16,
        signal: clamp(extreme * 0.8),
        raw: extreme === 0 ? "neutral" : extreme > 0 ? "capitulation" : "euphoria",
        note: extreme > 0 ? "Washout — fade the fear" : extreme < 0 ? "Froth — fade the greed" : "No extreme",
    });

    // --- Regime + magnitude modifiers -----------------------------------------
    // Dealer gamma sign sets the regime: long gamma (positive gxoi) dampens
    // moves and pins price (mean-reverting); short gamma amplifies them.
    const gxoi = u.gxoi;
    const gammaRegime = gxoi === 0 ? 0 : clamp(Math.sign(gxoi) * Math.min(1, Math.abs(gxoi) / dealerNorm(gxoi)));

    // Conviction booster: strong unusual flow + heavy option volume => trust the
    // directional read more.
    const flowIntensity = clamp(
        0.35 + 0.4 * Math.min(1, Math.abs(flowZ) / 2.5) + 0.25 * Math.min(1, u.option_volume / volNorm(u)),
        0,
        1,
    );

    // 1-day expected move as a fraction of spot from front-month annualized IV.
    const annualIv = front || u.volatility || 0.2;
    const dailySigma = annualIv / Math.sqrt(252);

    return { features, gammaRegime, flowIntensity, dailySigma };
}

// ---- helpers ------------------------------------------------------------------

function safePcr(put: number, call: number): number {
    if (call <= 0) return put > 0 ? 1.5 : 0.9;
    return put / call;
}

// Map an unbounded ratio-ish field into a signed [-1,1]-ish value. ConvexValue
// flowratio is already roughly centered near 0 for balanced flow; scale gently.
function normalizeRatio(r: number): number {
    if (!Number.isFinite(r)) return 0;
    if (Math.abs(r) <= 1.5) return r; // already signed & small
    return Math.sign(r) * (1 + Math.log10(Math.abs(r))); // compress large ratios
}

// Rough per-name normalizer for dealer greek-x-OI magnitudes so the signal is
// scale-free across large- and small-cap underlyings.
function dealerNorm(x: number): number {
    const a = Math.abs(x);
    if (a === 0) return 1;
    // one order of magnitude below the value itself => saturates near |1|
    return Math.pow(10, Math.floor(Math.log10(a)) + 1);
}

function volNorm(u: { call_volume: number; put_volume: number; option_volume: number }): number {
    return Math.max(1, u.option_volume * 0.6 + (u.call_volume + u.put_volume) * 0.4) || 1;
}

function firstTermVol(b: MarketBundle): number {
    return b.term.length ? num(b.term[0].volatility) : 0;
}
function lastTermVol(b: MarketBundle): number {
    return b.term.length ? num(b.term[b.term.length - 1].volatility) : 0;
}

function fmtSigned(v: number): string {
    if (!Number.isFinite(v) || v === 0) return "0";
    const a = Math.abs(v);
    const s = a >= 1e6 ? `${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}K` : a.toFixed(0);
    return v > 0 ? `+${s}` : `-${s}`;
}
