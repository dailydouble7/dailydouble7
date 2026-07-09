/* @refresh reload */
import { render } from "solid-js/web";
import { createSignal, createResource, For, Show, onCleanup, createEffect } from "solid-js";
import { getCommand, getCommandRoot, createWebSocket } from "@convexvalue/app";

import { fetchBundle, type MarketBundle } from "./data";
import { buildFeatures } from "./features";
import { predict, type Direction, type Horizon, type Prediction } from "./model";
import { mountFlowChart } from "./flowchart";
import { abbr, fixed, num, pct, signedPct } from "./format";
import "./styles.css";

// ---- command parsing ----------------------------------------------------------
// Grammar: `ofp <SYMBOL> [key=value ...]`  e.g. `ofp SPY exp=1-6`
interface Config {
    symbol: string;
}

function parseCommand(raw: string): Config {
    const command = raw && raw.trim() ? raw.trim() : "ofp SPY";
    const root = getCommandRoot(command);
    const tokens = command.slice(root.length).trim().split(/\s+/).filter(Boolean);
    const positional: string[] = [];
    for (const tok of tokens) {
        if (tok.indexOf("=") === -1) positional.push(tok);
    }
    return { symbol: (positional[0] || "SPY").toUpperCase() };
}

// ---- app ----------------------------------------------------------------------
function App() {
    const [config, setConfig] = createSignal<Config>(parseCommand(getCommand()));
    const [symbolInput, setSymbolInput] = createSignal(config().symbol);
    const [liveSpot, setLiveSpot] = createSignal<number | null>(null);
    const [liveChange, setLiveChange] = createSignal<number | null>(null);

    const [bundle, { refetch }] = createResource(config, (c) => fetchBundle(c.symbol));

    // Derived model output.
    const prediction = (): Prediction | null => {
        const b = bundle();
        if (!b) return null;
        return predict(buildFeatures(b));
    };

    // Live spot subscription over the app WebSocket.
    createEffect(() => {
        const sym = config().symbol;
        setLiveSpot(null);
        setLiveChange(null);
        let socket: WebSocket | null = null;
        try {
            socket = createWebSocket("/api/ws");
        } catch {
            return;
        }
        const onOpen = () => socket?.send(JSON.stringify([{ On: { s: [sym], v: ["price", "change"] } }]));
        const onMessage = (ev: MessageEvent) => {
            try {
                const msg = JSON.parse(ev.data);
                const updates = Array.isArray(msg) ? msg : [msg];
                for (const up of updates) {
                    if (!up || up.s !== sym || !Array.isArray(up.v)) continue;
                    for (const vo of up.v) {
                        if (vo && typeof vo.price === "number") setLiveSpot(vo.price);
                        if (vo && typeof vo.change === "number") setLiveChange(vo.change);
                    }
                }
            } catch {
                /* ignore malformed frames */
            }
        };
        socket.addEventListener("open", onOpen);
        socket.addEventListener("message", onMessage);
        onCleanup(() => {
            socket?.removeEventListener("open", onOpen);
            socket?.removeEventListener("message", onMessage);
            try {
                socket?.close();
            } catch {
                /* noop */
            }
        });
    });

    function submitSymbol(e: Event) {
        e.preventDefault();
        const s = symbolInput().trim().toUpperCase();
        if (s) setConfig({ symbol: s });
    }

    const spot = () => liveSpot() ?? bundle()?.spot ?? 0;

    return (
        <div class="app">
            <header class="topbar">
                <div class="brand">
                    <span class="logo">◆</span>
                    <div>
                        <div class="title">Options Flow Predictor</div>
                        <div class="subtitle">ensemble smart-money forecast · ConvexValue live flow</div>
                    </div>
                </div>
                <form class="symform" onSubmit={submitSymbol}>
                    <input
                        class="syminput"
                        value={symbolInput()}
                        onInput={(e) => setSymbolInput(e.currentTarget.value)}
                        placeholder="SYMBOL"
                        spellcheck={false}
                        autocomplete="off"
                    />
                    <button type="submit" class="go">Predict</button>
                    <button type="button" class="refresh" onClick={() => refetch()} title="Refresh">↻</button>
                </form>
            </header>

            <Show when={bundle.loading}>
                <div class="state loading">Loading {config().symbol} options flow…</div>
            </Show>
            <Show when={bundle.error as unknown}>
                <div class="state error">
                    Failed to load {config().symbol}. Check the symbol and that the app is launched from ConvexValue.
                </div>
            </Show>

            <Show when={!bundle.loading && !bundle.error && bundle()}>
                {(b) => (
                    <main class="grid">
                        <SpotBar bundle={b()} spot={spot()} change={liveChange()} live={liveSpot() !== null} />
                        <Show when={prediction()} fallback={<div class="state">No prediction.</div>}>
                            {(p) => (
                                <>
                                    <section class="cards">
                                        <For each={p().horizons}>{(h) => <HorizonCard h={h} spot={spot()} />}</For>
                                    </section>
                                    <ModelPanel p={p()} />
                                    <Breakdown p={p()} />
                                    <FlowPanel bundle={b()} />
                                    <TermPanel bundle={b()} />
                                </>
                            )}
                        </Show>
                    </main>
                )}
            </Show>

            <footer class="foot">
                Signals engineered from live ConvexValue flow, dealer greek-×-OI, and IV term structure.
                Transparent ensemble — not investment advice.
            </footer>
        </div>
    );
}

// ---- components ---------------------------------------------------------------

function dirClass(d: Direction): string {
    return d === "BULLISH" ? "bull" : d === "BEARISH" ? "bear" : "flat";
}
function dirArrow(d: Direction): string {
    return d === "BULLISH" ? "▲" : d === "BEARISH" ? "▼" : "▬";
}

function SpotBar(props: { bundle: MarketBundle; spot: number; change: number | null; live: boolean }) {
    const chg = () => (props.change ?? props.bundle.und.change) || 0;
    return (
        <section class="spotbar">
            <div class="spot-sym">{props.bundle.symbol}</div>
            <div class="spot-price">{fixed(props.spot, 2)}</div>
            <div class={`spot-chg ${chg() >= 0 ? "bull" : "bear"}`}>
                {chg() >= 0 ? "+" : ""}{fixed(chg(), 2)}
            </div>
            <Show when={props.live}><span class="live-dot" title="Live">●&nbsp;LIVE</span></Show>
            <div class="spot-meta">
                <span>PCR {fixed(props.bundle.und.put_call_ratio, 2)}</span>
                <span>IV {pct(props.bundle.und.front_volatility || props.bundle.und.volatility, 0)}</span>
                <span>Opt vol {abbr(props.bundle.und.option_volume)}</span>
            </div>
        </section>
    );
}

function HorizonCard(props: { h: Horizon; spot: number }) {
    const h = () => props.h;
    const target = () => props.spot * (1 + h().expectedMove);
    return (
        <div class={`card ${dirClass(h().direction)}`}>
            <div class="card-h">{h().days}-day</div>
            <div class="card-dir">
                <span class="arrow">{dirArrow(h().direction)}</span>
                <span>{h().direction}</span>
            </div>
            <div class="card-prob">{pct(h().probUp, 0)}<span class="lbl"> P(up)</span></div>
            <div class="card-move">{signedPct(h().expectedMove)} <span class="lbl">→ {fixed(target(), 2)}</span></div>
            <Meter value={h().confidence} label={`confidence ${pct(h().confidence, 0)}`} />
        </div>
    );
}

function Meter(props: { value: number; label: string; signed?: boolean }) {
    const v = () => Math.max(0, Math.min(1, props.signed ? (props.value + 1) / 2 : props.value));
    return (
        <div class="meter" title={props.label}>
            <div class="meter-track">
                <Show when={props.signed}><div class="meter-mid" /></Show>
                <div class="meter-fill" style={{ width: `${v() * 100}%` }} />
            </div>
            <div class="meter-label">{props.label}</div>
        </div>
    );
}

function ModelPanel(props: { p: Prediction }) {
    const p = () => props.p;
    const regimeTxt = () => {
        const g = p().gammaRegime;
        if (g > 0.15) return "Long gamma — dealers dampen & pin (mean-reverting)";
        if (g < -0.15) return "Short gamma — dealers amplify moves (trending)";
        return "Neutral gamma regime";
    };
    return (
        <section class="panel model">
            <h3>Ensemble internals</h3>
            <div class="submodels">
                <div class="submodel">
                    <div class="sm-name">Flow momentum <span class="tag">XGBoost analog</span></div>
                    <SignedMeter value={p().momentumScore} />
                </div>
                <div class="submodel">
                    <div class="sm-name">Dealer positioning <span class="tag">Random Forest analog</span></div>
                    <SignedMeter value={p().positioningScore} />
                </div>
            </div>
            <div class="regime">
                <span class="regime-lbl">Gamma regime</span>
                <span class="regime-txt">{regimeTxt()}</span>
            </div>
            <div class="regime">
                <span class="regime-lbl">Flow intensity</span>
                <Meter value={p().flowIntensity} label={pct(p().flowIntensity, 0)} />
            </div>
        </section>
    );
}

function SignedMeter(props: { value: number }) {
    const v = () => props.value;
    const cls = () => (v() > 0.05 ? "bull" : v() < -0.05 ? "bear" : "flat");
    return (
        <div class={`signed ${cls()}`}>
            <div class="signed-track">
                <div class="signed-zero" />
                <div
                    class="signed-fill"
                    style={{
                        width: `${Math.abs(v()) * 50}%`,
                        left: v() >= 0 ? "50%" : `${50 - Math.abs(v()) * 50}%`,
                    }}
                />
            </div>
            <div class="signed-val">{v() >= 0 ? "+" : ""}{fixed(v(), 2)}</div>
        </div>
    );
}

function Breakdown(props: { p: Prediction }) {
    const maxAbs = () => Math.max(...props.p.contributions.map((c) => Math.abs(c.contribution)), 0.0001);
    return (
        <section class="panel breakdown">
            <h3>Signal breakdown</h3>
            <table>
                <thead>
                    <tr>
                        <th>Signal</th><th>Model</th><th class="r">Value</th><th class="r">Signal</th><th>Contribution</th>
                    </tr>
                </thead>
                <tbody>
                    <For each={props.p.contributions}>{(c) => {
                        const f = c.feature;
                        const w = (Math.abs(c.contribution) / maxAbs()) * 100;
                        const bullish = c.contribution >= 0;
                        return (
                            <tr>
                                <td>
                                    <div class="sig-label">{f.label}</div>
                                    <div class="sig-note">{f.note}</div>
                                </td>
                                <td><span class={`pill ${f.model}`}>{f.model === "momentum" ? "MOM" : "POS"}</span></td>
                                <td class="r mono">{f.raw}</td>
                                <td class={`r mono ${f.signal >= 0 ? "bull" : "bear"}`}>{f.signal >= 0 ? "+" : ""}{fixed(f.signal, 2)}</td>
                                <td>
                                    <div class="contrib">
                                        <div class={`contrib-bar ${bullish ? "bull" : "bear"}`} style={{ width: `${w}%` }} />
                                    </div>
                                </td>
                            </tr>
                        );
                    }}</For>
                </tbody>
            </table>
        </section>
    );
}

function FlowPanel(props: { bundle: MarketBundle }) {
    let canvas: HTMLCanvasElement | undefined;
    createEffect(() => {
        const b = props.bundle; // re-run when bundle changes
        if (!canvas) return;
        const dispose = mountFlowChart(canvas, () => b.flow);
        onCleanup(dispose);
    });
    return (
        <section class="panel flow">
            <h3>Intraday net flow vs price</h3>
            <div class="chart-wrap"><canvas ref={canvas} /></div>
            <div class="legend">
                <span class="lg bull">▲ net buying</span>
                <span class="lg bear">▼ net selling</span>
                <span class="lg accent">— price</span>
            </div>
        </section>
    );
}

function TermPanel(props: { bundle: MarketBundle }) {
    return (
        <section class="panel term">
            <h3>IV term structure</h3>
            <Show when={props.bundle.term.length} fallback={<div class="empty">No term data.</div>}>
                <table>
                    <thead><tr><th>Exp</th><th class="r">IV</th><th class="r">PCR</th><th class="r">Opt vol</th></tr></thead>
                    <tbody>
                        <For each={props.bundle.term.slice(0, 8)}>{(t) => (
                            <tr>
                                <td class="mono">{num(t.expiration)}</td>
                                <td class="r mono">{pct(t.volatility, 1)}</td>
                                <td class="r mono">{fixed(t.put_call_ratio, 2)}</td>
                                <td class="r mono">{abbr(t.option_volume)}</td>
                            </tr>
                        )}</For>
                    </tbody>
                </table>
            </Show>
        </section>
    );
}

render(() => <App />, document.getElementById("root")!);
