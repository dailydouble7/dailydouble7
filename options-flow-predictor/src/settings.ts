// Runtime settings — lets a user paste their own ConvexValue API key (and,
// optionally, API / WebSocket base URLs) so the app can authenticate outside
// the ConvexValue app origin.
//
// The key is user-supplied at runtime and persisted only in this browser's
// localStorage. Nothing is hardcoded into the bundle. When no key is set the
// app falls back to same-origin cookie auth (the normal path when launched from
// ConvexValue), so setting a key is purely additive.
//
// The @convexvalue/app runtime turns `token` into an `Authorization: Bearer`
// header on REST calls and a `?token=` param on the WebSocket; `apiBaseUrl` /
// `wsBaseUrl` route those calls when the app is served off-origin.

import { configureRuntime } from "@convexvalue/app";

const STORAGE_KEY = "ofp:runtime";

export interface RuntimeSettings {
    token?: string;
    apiBaseUrl?: string;
    wsBaseUrl?: string;
}

function hasStorage(): boolean {
    try {
        return typeof localStorage !== "undefined";
    } catch {
        return false;
    }
}

export function loadSettings(): RuntimeSettings {
    if (!hasStorage()) return {};
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return {};
        const p = JSON.parse(raw) as Record<string, unknown>;
        return {
            token: typeof p.token === "string" ? p.token : undefined,
            apiBaseUrl: typeof p.apiBaseUrl === "string" ? p.apiBaseUrl : undefined,
            wsBaseUrl: typeof p.wsBaseUrl === "string" ? p.wsBaseUrl : undefined,
        };
    } catch {
        return {};
    }
}

// Trim, drop empties, persist, and apply. Returns the cleaned settings.
export function saveSettings(input: RuntimeSettings): RuntimeSettings {
    const clean: RuntimeSettings = {};
    const token = input.token?.trim();
    const apiBaseUrl = input.apiBaseUrl?.trim();
    const wsBaseUrl = input.wsBaseUrl?.trim();
    if (token) clean.token = token;
    if (apiBaseUrl) clean.apiBaseUrl = apiBaseUrl;
    if (wsBaseUrl) clean.wsBaseUrl = wsBaseUrl;

    if (hasStorage()) {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(clean));
        } catch {
            /* ignore quota / disabled storage */
        }
    }
    applySettings(clean);
    return clean;
}

export function clearSettings(): void {
    if (hasStorage()) {
        try {
            localStorage.removeItem(STORAGE_KEY);
        } catch {
            /* noop */
        }
    }
    // Reset manual overrides so same-origin cookie auth resumes.
    configureRuntime({});
}

// Only override the runtime with values the user actually provided, so we never
// clobber same-origin cookie auth when a field is left blank.
export function applySettings(s: RuntimeSettings = loadSettings()): void {
    const cfg: Record<string, string> = {};
    if (s.token) cfg.token = s.token;
    if (s.apiBaseUrl) cfg.apiBaseUrl = s.apiBaseUrl;
    if (s.wsBaseUrl) cfg.wsBaseUrl = s.wsBaseUrl;
    configureRuntime(cfg);
}

export function hasKey(): boolean {
    return !!loadSettings().token;
}

// Apply any stored settings as soon as this module loads, before the first
// data call fires.
applySettings();
