// Offline sanity check for the feature engineering + ensemble model. Runs
// synthetic bullish / bearish / neutral bundles through the same code path the
// app uses and asserts the outputs are sane (no NaN, directions respond to
// inputs, probabilities in range). Not a data-connected test — it validates the
// pure model logic that ships in the app.
import { buildFeatures } from "../src/features";
import { predict } from "../src/model";
import type { MarketBundle, UndSnapshot, FlowPoint } from "../src/data";

function und(over: Partial<UndSnapshot>): UndSnapshot {
    const base: Record<string, number> = {};
    const keys = [
        "price", "change", "put_call_ratio", "call_volume", "put_volume", "option_volume",
        "volatility", "front_volatility", "back_volatility", "flowratio", "vflowratio",
        "value", "volm", "value_buy", "value_sell", "value_call_buy", "value_put_buy",
        "value_call_sell", "value_put_sell", "deltas_buy", "deltas_sell",
        "deltas_call_buy", "deltas_put_buy", "deltas_call_sell", "deltas_put_sell",
        "gammas_buy", "gammas_sell", "gxoi", "dxoi", "vxoi", "charmxoi", "vannaxoi",
        "gxvolm", "dxvolm",
    ];
    keys.forEach((k) => (base[k] = 0));
    base.price = 100;
    base.volatility = 0.25;
    base.front_volatility = 0.25;
    base.back_volatility = 0.27;
    base.option_volume = 500000;
    base.call_volume = 300000;
    base.put_volume = 200000;
    base.put_call_ratio = 0.9;
    return { symbol: "TEST", ...(base as unknown as UndSnapshot), ...over };
}

function flow(trend: number): FlowPoint[] {
    const out: FlowPoint[] = [];
    for (let i = 0; i < 40; i++) {
        out.push({
            t: i,
            price: 100 + Math.sin(i / 5) + trend * i * 0.02,
            flownet: trend * i * 1000 + Math.sin(i) * 500,
            vflownet: trend * i * 200,
            callBs: trend * 1000,
            putBs: -trend * 500,
        });
    }
    return out;
}

function bundle(over: Partial<UndSnapshot>, trend: number): MarketBundle {
    return {
        symbol: "TEST",
        spot: 100,
        und: und(over),
        term: [
            { expiration: 7, volatility: 0.28, call_volume: 1e5, put_volume: 8e4, put_call_ratio: 0.8, option_volume: 1.8e5 },
            { expiration: 30, volatility: 0.26, call_volume: 2e5, put_volume: 1.5e5, put_call_ratio: 0.75, option_volume: 3.5e5 },
        ],
        flow: flow(trend),
    };
}

const cases: { name: string; b: MarketBundle; expect: "up" | "down" | "any" }[] = [
    {
        name: "strong bullish flow",
        b: bundle(
            {
                put_call_ratio: 0.55,
                deltas_buy: 800000, deltas_sell: 200000,
                flowratio: 0.8, dxoi: -5e8, gxoi: -2e8,
            },
            1,
        ),
        expect: "up",
    },
    {
        name: "strong bearish flow",
        b: bundle(
            {
                put_call_ratio: 1.4,
                deltas_buy: 150000, deltas_sell: 700000,
                flowratio: -0.7, dxoi: 5e8, gxoi: -2e8,
            },
            -1,
        ),
        expect: "down",
    },
    { name: "balanced / neutral", b: bundle({}, 0), expect: "any" },
    { name: "empty flow + zero greeks", b: { ...bundle({}, 0), flow: [], term: [] }, expect: "any" },
];

let failures = 0;
for (const c of cases) {
    const fs = buildFeatures(c.b);
    const p = predict(fs);
    const finite = p.horizons.every(
        (h) => Number.isFinite(h.probUp) && Number.isFinite(h.expectedMove) && Number.isFinite(h.confidence) &&
            h.probUp >= 0 && h.probUp <= 1 && h.confidence >= 0 && h.confidence <= 1,
    );
    const h3 = p.horizons[1];
    const dirOk =
        c.expect === "any" ? true : c.expect === "up" ? h3.probUp > 0.5 : h3.probUp < 0.5;

    const ok = finite && dirOk;
    if (!ok) failures++;
    const line = p.horizons
        .map((h) => `${h.days}d ${h.direction.padEnd(7)} P=${(h.probUp * 100).toFixed(0)}% mv=${(h.expectedMove * 100).toFixed(2)}%`)
        .join("  |  ");
    console.log(`${ok ? "PASS" : "FAIL"}  ${c.name}`);
    console.log(`        mom=${p.momentumScore.toFixed(2)} pos=${p.positioningScore.toFixed(2)} gamma=${p.gammaRegime.toFixed(2)} intensity=${p.flowIntensity.toFixed(2)}`);
    console.log(`        ${line}`);
}

if (failures) {
    console.error(`\n${failures} case(s) failed`);
    process.exit(1);
}
console.log("\nAll model sanity checks passed.");
