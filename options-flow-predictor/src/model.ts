// Ensemble prediction model.
//
// The notebook combines two learners — a trend-oriented gradient-boosted model
// (XGBoost) and a regime/mean-reversion model (Random Forest) — into a
// performance-weighted ensemble that emits directional forecasts at the 1-, 3-
// and 5-day horizons with confidence.
//
// A cvforge app is static client-side code with no pickled model artifact and
// no training set (ConvexValue serves *live* data, not the notebook's labeled
// history). So this is a transparent re-implementation of that ensemble's
// decision logic: two additive sub-models over the engineered signals, blended
// with horizon-dependent weights, modulated by the dealer-gamma regime. Every
// term is inspectable in the UI — nothing is a black box.

import type { Feature, FeatureSet, SubModel } from "./features";
import { clamp, sigmoid, tanh } from "./format";

export type Direction = "BULLISH" | "BEARISH" | "NEUTRAL";

export interface Horizon {
    days: number;
    direction: Direction;
    probUp: number; // P(up) in [0,1]
    confidence: number; // [0,1]
    expectedMove: number; // signed fraction of spot
    momentum: number; // sub-model score at this horizon
    positioning: number; // sub-model score at this horizon
    ensemble: number; // blended score, [-1,1]
}

export interface Contribution {
    feature: Feature;
    contribution: number; // signed weight*signal used at the 3-day reference
}

export interface Prediction {
    horizons: Horizon[];
    contributions: Contribution[];
    momentumScore: number; // raw sub-model score (pre horizon-weighting)
    positioningScore: number;
    gammaRegime: number;
    flowIntensity: number;
}

// Weighted, tanh-squashed score for one sub-model over its features.
function subScore(features: Feature[], model: SubModel): number {
    const members = features.filter((f) => f.model === model);
    const total = members.reduce((a, f) => a + f.weight, 0) || 1;
    const raw = members.reduce((a, f) => a + f.weight * f.signal, 0) / total;
    // gain > 1 sharpens conviction the way a boosted ensemble does vs a simple
    // weighted average.
    return tanh(1.6 * raw);
}

// Ensemble blend weights per horizon: momentum dominates the near term, dealer
// positioning / mean-reversion dominates further out. Momentum also decays with
// horizon because a flow burst says more about tomorrow than about next week.
const HORIZONS: { days: number; wMom: number; wPos: number; momDecay: number }[] = [
    { days: 1, wMom: 0.65, wPos: 0.35, momDecay: 1.0 },
    { days: 3, wMom: 0.5, wPos: 0.5, momDecay: 0.8 },
    { days: 5, wMom: 0.35, wPos: 0.65, momDecay: 0.62 },
];

const K = 2.3; // logistic steepness mapping ensemble score -> probability

export function predict(fs: FeatureSet): Prediction {
    const momentumScore = subScore(fs.features, "momentum");
    const positioningScore = subScore(fs.features, "positioning");

    // Long-gamma regime (gammaRegime > 0) dampens directional moves and adds a
    // mean-reversion pull that opposes momentum; short-gamma amplifies momentum.
    const gammaAmp = 1 - 0.4 * fs.gammaRegime; // (0.6 .. 1.4)
    const meanRevPull = -0.25 * fs.gammaRegime * momentumScore;

    const horizons: Horizon[] = HORIZONS.map((h) => {
        const mom = momentumScore * h.momDecay * gammaAmp;
        const pos = positioningScore + meanRevPull * (h.wPos); // reversion grows with horizon
        const ensemble = clamp(h.wMom * mom + h.wPos * pos);

        const probUp = sigmoid(K * ensemble);
        const confidence = clamp(Math.abs(2 * probUp - 1) * (0.55 + 0.45 * fs.flowIntensity), 0, 1);

        const direction: Direction =
            probUp > 0.55 ? "BULLISH" : probUp < 0.45 ? "BEARISH" : "NEUTRAL";

        // Expected move: horizon-scaled 1-day sigma, tilted by direction and
        // scaled by conviction.
        const sigmaH = fs.dailySigma * Math.sqrt(h.days);
        const tilt = (probUp - 0.5) * 2; // [-1,1]
        const expectedMove = tilt * sigmaH * (0.35 + 0.65 * confidence);

        return {
            days: h.days,
            direction,
            probUp,
            confidence,
            expectedMove,
            momentum: mom,
            positioning: pos,
            ensemble,
        };
    });

    // Contributions at the 3-day reference blend, for the breakdown table.
    const ref = HORIZONS[1];
    const contributions: Contribution[] = fs.features
        .map((f) => {
            const blendW = f.model === "momentum" ? ref.wMom : ref.wPos;
            return { feature: f, contribution: blendW * f.weight * f.signal };
        })
        .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));

    return {
        horizons,
        contributions,
        momentumScore,
        positioningScore,
        gammaRegime: fs.gammaRegime,
        flowIntensity: fs.flowIntensity,
    };
}
