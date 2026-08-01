"""Command line interface for spxvol."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta

from .bars import Bar
from .black76 import implied_vol, price as bs_price, vega
from .forward import ParityQuote, solve_forward
from .occ import OptionContract
from .pipeline import (
    ScanConfig,
    build_history,
    infer_center,
    scan_session,
)
from .signal import SkewHistory, evaluate_print
from .smile import fit_smile, make_point
from .source import CvForgeSource, MCPClient, SourceError
from .tenor import tenor_years
from .trading_calendar import EASTERN


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spxvol",
        description="Implied vol and rich-trade detection for SPX options from cvforge bars.",
    )
    parser.add_argument("--cache-dir", default=None, help="override the bar cache directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="validate the math end to end offline (no API key needed)")
    sub.add_parser("tools", help="list every MCP tool the key can reach")
    sub.add_parser("probe", help="check whether the account can serve underlying/index prices")

    scan = sub.add_parser("scan", help="scan one session for large prints that paid up")
    scan.add_argument("--root", default="SPXW")
    scan.add_argument("--expiry", required=True, type=_parse_date)
    scan.add_argument("--session", required=True, type=_parse_date)
    scan.add_argument("--center", type=float, default=None,
                      help="approximate index level; omit with --infer-center")
    scan.add_argument("--infer-center", action="store_true",
                      help="locate the forward from a coarse grid via C-P = 0")
    scan.add_argument("--band", type=float, default=0.05, help="strike band as a fraction")
    scan.add_argument("--step", type=float, default=25.0, help="strike spacing")
    scan.add_argument("--interval", default="1m")
    scan.add_argument("--min-volume", type=float, default=250.0)
    scan.add_argument("--min-avg-size", type=float, default=50.0)
    scan.add_argument("--history", default=None, help="path to a saved history JSON for layer 2")
    scan.add_argument("--limit", type=int, default=25)
    scan.add_argument("--json", action="store_true", help="emit rows as JSON")
    scan.add_argument("--quiet", action="store_true")

    hist = sub.add_parser("history", help="build the layer-2 trailing skew distribution")
    hist.add_argument("--root", default="SPXW")
    hist.add_argument("--expiries", required=True,
                      help="comma-separated expiration dates")
    hist.add_argument("--center", type=float, required=True)
    hist.add_argument("--start", required=True, type=_parse_date)
    hist.add_argument("--end", required=True, type=_parse_date)
    hist.add_argument("--band", type=float, default=0.08)
    hist.add_argument("--step", type=float, default=25.0)
    hist.add_argument("--out", required=True, help="where to write the history JSON")

    return parser


def _source(args: argparse.Namespace) -> CvForgeSource:
    return CvForgeSource(cache_dir=args.cache_dir)


def cmd_tools(args: argparse.Namespace) -> int:
    client = MCPClient()
    tools = client.list_tools()
    print(f"{len(tools)} tools available:\n")
    for tool in tools:
        desc = (tool.get("description") or "").strip().replace("\n", " ")
        print(f"  {tool.get('name')}\n      {desc[:200]}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    source = _source(args)
    probe = source.probe_underlying_price()
    print(probe.summary())
    print()
    if probe.has_candidate:
        print("Candidates found. spxvol does not need any of them -- the forward comes")
        print("from put-call parity -- but a spot feed enables spot-delta conventions")
        print("and an implied dividend yield via black76.dividend_discount().")
    else:
        print("No underlying-price capability detected. This is not a blocker:")
        print("parity recovers both the forward and the discount from the options alone.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    source = _source(args)
    progress = None if args.quiet else (lambda msg: print(msg, file=sys.stderr))

    center = args.center
    if args.infer_center or center is None:
        if center is None:
            print("--center or --infer-center with an approximate level is required",
                  file=sys.stderr)
            return 2
        found = infer_center(source, args.root, args.expiry, args.session, center)
        if found is None:
            print("could not infer a forward; pass an explicit --center", file=sys.stderr)
            return 1
        if progress:
            progress(f"inferred forward ~ {found:.2f}")
        center = found

    history = None
    if args.history:
        history = _load_history(args.history)

    config = ScanConfig(
        root=args.root, expiration=args.expiry, session=args.session,
        center=center, band_pct=args.band, strike_step=args.step,
        interval=args.interval, min_volume=args.min_volume, min_avg_size=args.min_avg_size,
    )
    scan = scan_session(source, config, history=history, progress=progress)

    if args.json:
        print(json.dumps([f.to_row() for f in scan.flags[:args.limit]], indent=2))
        return 0

    print(f"\ncontracts with data : {scan.contracts_fetched}")
    print(f"minutes examined    : {scan.minutes_examined}")
    print(f"  with a forward    : {scan.minutes_with_forward}")
    print(f"  with a smile      : {scan.minutes_with_smile}")
    print(f"flags               : {len(scan.flags)}")
    if scan.rejects:
        top = sorted(scan.rejects.items(), key=lambda kv: -kv[1])[:6]
        print("rejects             : " + ", ".join(f"{k}={v}" for k, v in top))

    if not scan.flags:
        return 0

    print(f"\nTop {min(args.limit, len(scan.flags))} prints by score:\n")
    header = (f"{'time':<9}{'contract':<24}{'sz':>7}{'qual':>8}{'px':>9}"
              f"{'ivΔ':>8}{'$over':>12}{'skew%':>7}{'score':>8}")
    print(header)
    print("-" * len(header))
    for flag in scan.flags[:args.limit]:
        skew = "" if math.isnan(flag.skew_percentile) else f"{flag.skew_percentile * 100:.0f}"
        label = f"{flag.strike:g}{'C' if flag.is_call else 'P'}"
        print(f"{flag.timestamp.astimezone(EASTERN):%H:%M:%S} "
              f"{label:<24}{flag.contracts:>7.0f}{flag.print_quality:>8}"
              f"{flag.trade_price:>9.2f}{flag.residual_vol_pts:>+8.2f}"
              f"{flag.dollars_over_curve:>12,.0f}{skew:>7}{flag.score:>8.2f}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    source = _source(args)
    expiries = [_parse_date(x.strip()) for x in args.expiries.split(",") if x.strip()]
    progress = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    history = build_history(source, args.root, expiries, args.center, args.start, args.end,
                            band_pct=args.band, strike_step=args.step, progress=progress)
    _save_history(history, args.out)
    total = sum(len(v) for v in history.records.values())
    print(f"wrote {args.out}: {len(history.records)} buckets, {total} observations")
    return 0


def _save_history(history: SkewHistory, path: str) -> None:
    payload = {
        "records": [
            {"bucket": key[0], "delta": key[1], "is_call": key[2],
             "values": [[day.isoformat(), value] for day, value in rows]}
            for key, rows in history.records.items()
        ],
        "atm": {bucket: [[day.isoformat(), value] for day, value in rows]
                for bucket, rows in history.atm_records.items()},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _load_history(path: str) -> SkewHistory:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    history = SkewHistory()
    for entry in payload.get("records", []):
        key = (entry["bucket"], float(entry["delta"]), bool(entry["is_call"]))
        history.records[key] = [(_parse_date(d), float(v)) for d, v in entry["values"]]
    for bucket, rows in payload.get("atm", {}).items():
        history.atm_records[bucket] = [(_parse_date(d), float(v)) for d, v in rows]
    return history


def cmd_selftest(args: argparse.Namespace) -> int:
    """Round-trip the whole chain on synthetic data with a known answer.

    Builds a surface with a chosen forward, discount and skew, prices a chain
    from it, then checks that parity recovers (F, D), that inversion recovers
    the input vols, and that a deliberately rich print is measured at the size
    it was made rich by. Runs entirely offline.
    """
    true_forward = 6000.0
    true_discount = 0.9985
    expiry = date.today() + timedelta(days=30)
    while expiry.weekday() >= 5:
        expiry += timedelta(days=1)
    now = datetime.now(tz=EASTERN).replace(hour=11, minute=0, second=0, microsecond=0)
    reference = OptionContract("SPXW", expiry, True, true_forward)
    tenor = tenor_years(reference, now)
    if tenor <= 0:
        print("selftest needs a future expiry", file=sys.stderr)
        return 1

    def true_vol(strike: float) -> float:
        k = math.log(strike / true_forward)
        return math.sqrt(max(0.04 - 0.35 * k + 1.8 * k * k, 1e-4))

    strikes = [true_forward + 25.0 * i for i in range(-24, 25)]

    quotes = [
        ParityQuote(
            k,
            bs_price(true_forward, k, tenor, true_vol(k), True, true_discount),
            bs_price(true_forward, k, tenor, true_vol(k), False, true_discount),
        )
        for k in strikes
    ]
    solution = solve_forward(quotes)
    ok = True

    print("1. put-call parity recovery")
    if not solution.ok or solution.forward is None or solution.discount is None:
        print(f"   FAIL: {solution.reason}")
        return 1
    f_err = abs(solution.forward - true_forward)
    d_err = abs(solution.discount - true_discount)
    print(f"   forward  {solution.forward:.6f}  (error {f_err:.2e})")
    print(f"   discount {solution.discount:.9f}  (error {d_err:.2e})")
    print(f"   R^2 {solution.r_squared:.10f}, confidence {solution.confidence}")
    ok &= f_err < 1e-6 and d_err < 1e-9

    print("2. implied-vol inversion")
    worst = 0.0
    for k in strikes:
        is_call = k >= true_forward
        px = bs_price(true_forward, k, tenor, true_vol(k), is_call, true_discount)
        result = implied_vol(px, true_forward, k, tenor, is_call, true_discount)
        if not result.ok or result.vol is None:
            print(f"   FAIL at K={k}: {result.reason}")
            ok = False
            continue
        worst = max(worst, abs(result.vol - true_vol(k)))
    print(f"   worst absolute vol error across {len(strikes)} strikes: {worst:.3e}")
    ok &= worst < 1e-7

    print("3. smile fit")
    points = []
    for k in strikes:
        is_call = k >= true_forward
        vol = true_vol(k)
        points.append(make_point(k, true_forward, vol,
                                 vega(true_forward, k, tenor, vol, true_discount),
                                 tag=f"K{k:g}"))
    fit = fit_smile(points, tenor, true_forward)
    print(f"   points {fit.n_points}, rmse {fit.rmse_vol * 100:.4f} vol pts, "
          f"atm {fit.atm_iv * 100:.2f}")
    k25 = fit.strike_at_delta(-0.25, False)
    print(f"   25-delta put strike {k25:.1f}, iv {fit.iv_at_strike(k25) * 100:.2f} "
          f"({(fit.iv_at_strike(k25) - fit.atm_iv) * 100:+.2f} vs atm)")
    ok &= fit.rmse_vol < 0.01 and k25 < true_forward

    print("4. rich-print detection (leave-one-out)")
    rich_strike = true_forward - 300.0
    fair_vol = true_vol(rich_strike)
    paid_vol = fair_vol + 0.02  # 2 vol points over fair
    paid_price = bs_price(true_forward, rich_strike, tenor, paid_vol, False, true_discount)
    ticker = OptionContract("SPXW", expiry, False, rich_strike).ticker
    bar = Bar(ticker=ticker, timestamp_ms=int(now.timestamp() * 1000),
              open=paid_price, high=paid_price, low=paid_price, close=paid_price,
              volume=2500, vwap=paid_price, transactions=1)
    # The print's own contract is present in the fit and must be excluded by
    # evaluate_print, otherwise it drags the curve toward its own price.
    points.append(make_point(rich_strike, true_forward, paid_vol,
                             vega(true_forward, rich_strike, tenor, paid_vol, true_discount),
                             tag=ticker))
    flag = evaluate_print(bar, OptionContract("SPXW", expiry, False, rich_strike),
                          forward=true_forward, discount=true_discount, tenor=tenor,
                          sessions=20, points=points, forward_confidence="high")
    if flag is None:
        print("   FAIL: print was not evaluable")
        return 1
    print(f"   quality {flag.print_quality}, size {flag.contracts:.0f}, "
          f"delta {flag.delta:+.3f}")
    print(f"   residual {flag.residual_vol_pts:+.3f} vol pts (injected +2.000)")
    print(f"   $ over curve {flag.dollars_over_curve:,.0f}, hint {flag.direction_hint}")
    ok &= abs(flag.residual_vol_pts - 2.0) < 0.15
    ok &= flag.direction_hint == "buyer_paid_up"

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "selftest": cmd_selftest,
        "tools": cmd_tools,
        "probe": cmd_probe,
        "scan": cmd_scan,
        "history": cmd_history,
    }
    try:
        return handlers[args.command](args)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
