// Small formatting helpers. API rows are untrusted external data, so every
// numeric read goes through `num()` which coerces null/undefined/NaN to a
// safe fallback before it ever reaches a calculation or the DOM.

export function num(value: unknown, fallback = 0): number {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
}

export function pct(value: number, digits = 1): string {
    if (!Number.isFinite(value)) return "—";
    return `${(value * 100).toFixed(digits)}%`;
}

export function signedPct(value: number, digits = 2): string {
    if (!Number.isFinite(value)) return "—";
    const s = (value * 100).toFixed(digits);
    return value > 0 ? `+${s}%` : `${s}%`;
}

// Compact human abbreviation for large dollar/notional values.
export function abbr(value: number): string {
    if (!Number.isFinite(value)) return "—";
    const sign = value < 0 ? "-" : "";
    const a = Math.abs(value);
    if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(2)}M`;
    if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}K`;
    return `${sign}${a.toFixed(0)}`;
}

export function fixed(value: number, digits = 2): string {
    if (!Number.isFinite(value)) return "—";
    return value.toFixed(digits);
}

export function clamp(value: number, lo = -1, hi = 1): number {
    if (!Number.isFinite(value)) return 0;
    return Math.max(lo, Math.min(hi, value));
}

export function tanh(x: number): number {
    if (!Number.isFinite(x)) return 0;
    return Math.tanh(x);
}

export function sigmoid(x: number): number {
    return 1 / (1 + Math.exp(-x));
}
