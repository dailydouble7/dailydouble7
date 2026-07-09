// Intraday net-flow vs price canvas chart. Kept dependency-light and
// DPR-aware: a net-flow area (green above zero / red below) with the price line
// overlaid on a secondary scale. Redraws on resize via ResizeObserver.

import { extent } from "d3-array";
import type { FlowPoint } from "./data";

const CSS = getComputedStyle(document.documentElement);
function cssVar(name: string, fallback: string): string {
    const v = CSS.getPropertyValue(name).trim();
    return v || fallback;
}

export function mountFlowChart(canvas: HTMLCanvasElement, points: () => FlowPoint[]): () => void {
    const ctx = canvas.getContext("2d")!;
    let raf = 0;

    function draw() {
        raf = 0;
        const data = points();
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const w = Math.max(1, Math.floor(rect.width));
        const h = Math.max(1, Math.floor(rect.height));
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        const bg = cssVar("--panel", "#12161c");
        ctx.fillStyle = bg;
        ctx.fillRect(0, 0, w, h);

        if (data.length < 2) {
            ctx.fillStyle = cssVar("--muted", "#6b7684");
            ctx.font = "12px ui-monospace, monospace";
            ctx.textAlign = "center";
            ctx.fillText("No intraday flow data", w / 2, h / 2);
            return;
        }

        const padL = 8, padR = 8, padT = 10, padB = 14;
        const iw = w - padL - padR;
        const ih = h - padT - padB;

        const n = data.length;
        const x = (i: number) => padL + (iw * i) / (n - 1);

        // Flow scale (symmetric around zero).
        const flowVals = data.map((d) => d.flownet);
        const [fmin = -1, fmax = 1] = extent(flowVals) as [number, number];
        const fAbs = Math.max(Math.abs(fmin), Math.abs(fmax), 1);
        const yFlow = (v: number) => padT + ih / 2 - (v / fAbs) * (ih / 2);

        // Zero line.
        ctx.strokeStyle = cssVar("--grid", "#232a33");
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(padL, yFlow(0));
        ctx.lineTo(w - padR, yFlow(0));
        ctx.stroke();

        // Net-flow area.
        const up = cssVar("--bull", "#2fd08a");
        const dn = cssVar("--bear", "#ff5d6c");
        for (let i = 0; i < n - 1; i++) {
            const v = data[i].flownet;
            ctx.fillStyle = v >= 0 ? hexA(up, 0.35) : hexA(dn, 0.35);
            ctx.beginPath();
            ctx.moveTo(x(i), yFlow(0));
            ctx.lineTo(x(i), yFlow(v));
            ctx.lineTo(x(i + 1), yFlow(data[i + 1].flownet));
            ctx.lineTo(x(i + 1), yFlow(0));
            ctx.closePath();
            ctx.fill();
        }

        // Price line on its own scale.
        const priceVals = data.map((d) => d.price).filter((p) => p > 0);
        if (priceVals.length > 1) {
            const [pmin = 0, pmax = 1] = extent(priceVals) as [number, number];
            const span = pmax - pmin || 1;
            const yPrice = (p: number) => padT + ih - ((p - pmin) / span) * ih;
            ctx.strokeStyle = cssVar("--accent", "#e8b84b");
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            let started = false;
            data.forEach((d, i) => {
                if (d.price <= 0) return;
                const px = x(i), py = yPrice(d.price);
                if (!started) {
                    ctx.moveTo(px, py);
                    started = true;
                } else ctx.lineTo(px, py);
            });
            ctx.stroke();
        }
    }

    function schedule() {
        if (!raf) raf = requestAnimationFrame(draw);
    }

    const ro = new ResizeObserver(schedule);
    ro.observe(canvas);
    schedule();

    return () => {
        ro.disconnect();
        if (raf) cancelAnimationFrame(raf);
    };
}

// Apply an alpha to a #rrggbb color.
function hexA(hex: string, a: number): string {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
