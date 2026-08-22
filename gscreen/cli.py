"""Command line entry point.

Sources are chosen at runtime and independently:

    --universe      eodhd | static | fixture
    --prices        eodhd | yahoo  | fixture
    --fundamentals  eodhd | edgar  | fixture

Or pick a preset and override any single piece:

    python -m gscreen.cli screen --preset offline
    python -m gscreen.cli screen --preset free
    python -m gscreen.cli screen --preset free --prices eodhd
    python -m gscreen.cli backtest --preset free
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backtest import backtest, report
from .dates import resolve_as_of
from .llm import build_prompt, call_model, parse_response, validate_grounding
from .providers import PRESETS, SOURCES, build_provider
from .screen import ScreenConfig, run_screen

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
UNIVERSE_FILE = ROOT / "universe.txt"


def resolve_sources(args) -> tuple[str, str, str]:
    """Preset supplies defaults; any explicit flag wins over it."""
    universe, prices, fundamentals = PRESETS[args.preset]
    return (
        args.universe or universe,
        args.prices or prices,
        args.fundamentals or fundamentals,
    )


def doctor(args) -> None:
    """Probe each source once and report exactly what came back.

    A screen that rejects everything with the same error tells you nothing.
    This tells you which of the three sources is broken, and how.
    """
    from .providers import (
        EdgarFundamentals,
        StaticUniverse,
        StooqPrices,
        TiingoPrices,
        YahooPrices,
    )

    ticker = "AAPL"
    as_of = args.as_of
    print(f"probing sources with {ticker}, as_of={as_of}\n")

    def check(label, fn):
        try:
            result = fn()
            print(f"  OK    {label:<22} {result}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {label:<22} {exc}")

    check(
        "universe (static)",
        lambda: f"{len(StaticUniverse(UNIVERSE_FILE).universe(1000))} tickers",
    )
    def describe_series(source):
        rows = source.eod_prices(ticker, "2000-01-01", as_of)
        if not rows:
            return "0 rows"
        enough = "enough" if len(rows) >= 274 else "TOO FEW for 12-1"
        return f"{len(rows)} rows, {rows[0]['date']}..{rows[-1]['date']} ({enough})"

    check("prices (tiingo)", lambda: describe_series(TiingoPrices()))
    check("prices (stooq)", lambda: describe_series(StooqPrices()))
    check("prices (yahoo)", lambda: describe_series(YahooPrices()))

    edgar = EdgarFundamentals()
    check("edgar ticker index", lambda: f"CIK {edgar.cik_for(ticker)}")

    def stale():
        unknown = StaticUniverse(UNIVERSE_FILE).unknown_to_sec(edgar)
        return (
            "all tickers known to SEC"
            if not unknown
            else f"UNKNOWN (renamed or delisted?): {', '.join(unknown)}"
        )

    check("universe.txt freshness", stale)
    check(
        "edgar fundamentals",
        lambda: (
            f"{len(edgar.fundamentals(ticker, as_of).annual_revenue)} annual "
            "revenue periods"
        ),
    )
    print(
        "\nA 403 from EDGAR usually means SEC_USER_AGENT is unset or generic."
        "\nAn HTML body from Stooq or a 429 from Yahoo means the host refused"
        "\nthis IP; both fingerprint by IP. Tiingo uses a token and does not."
        "\n12-1 momentum needs 274 daily closes (~13 months); a short series"
        "\nmeans the price source truncated the range, not that the screen failed."
    )


def explain(provider, ticker: str, as_of: str) -> None:
    """Dump the working for one company.

    A rejection like "only 1 consecutive growth quarters" is a conclusion, not
    evidence. This prints the quarterly revenue series and each YoY pairing so
    a surprising verdict can be checked rather than believed.
    """
    from .screen import _quarterly_yoy, extract_fundamental_facts

    f = provider.fundamentals(ticker, as_of)
    print(f"{ticker}  source={f.source}  point_in_time={f.point_in_time}")
    print(f"name: {f.name}\n")

    print("annual revenue (oldest first):")
    for value in f.annual_revenue:
        print(f"   {value:>18,.0f}")

    print("\nquarterly revenue as filed:")
    for period, value in f.quarterly_revenue:
        print(f"   {period}  {value:>18,.0f}")

    yoy = _quarterly_yoy(f.quarterly_revenue)
    print("\nYoY per quarter (newest first), matched by date:")
    periods = [p for p, _ in sorted(f.quarterly_revenue, reverse=True)]
    for period, growth in zip(periods, yoy):
        shown = "n/a" if growth is None else f"{growth * 100:+.1f}%"
        print(f"   {period}  {shown}")
    if not yoy:
        print("   (none - no quarter had a match within 35 days of one year earlier)")

    facts = extract_fundamental_facts(f, as_of)
    print("\nderived:")
    for key in (
        "revenue_cagr_3y", "consecutive_growth_quarters", "rule_of_40",
        "share_count_growth", "shares_outstanding", "market_cap",
        "net_debt_to_ebitda", "days_since_earnings",
    ):
        print(f"   {key:<28} {facts.get(key)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="gscreen")
    parser.add_argument(
        "command",
        choices=[
            "screen", "backtest", "prompt", "sources", "doctor", "universe",
            "explain",
        ],
    )
    parser.add_argument("--preset", default="offline", choices=sorted(PRESETS))
    parser.add_argument("--universe", choices=SOURCES["universe"])
    parser.add_argument("--prices", choices=SOURCES["prices"])
    parser.add_argument("--fundamentals", choices=SOURCES["fundamentals"])
    parser.add_argument(
        "--as-of", default=None, help="defaults to the last trading day"
    )
    parser.add_argument("--limit", type=int, default=None, help="cap the universe size")
    parser.add_argument("--ticker", help="for the explain command")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--ignore-lookahead",
        action="store_true",
        help="disable the point-in-time guard (produces a contaminated backtest)",
    )
    args = parser.parse_args()

    if args.command == "sources":
        for name, combo in sorted(PRESETS.items()):
            print(f"{name:<9} universe={combo[0]:<8} prices={combo[1]:<8} fundamentals={combo[2]}")
        print()
        for capability, options in SOURCES.items():
            print(f"{capability:<13} {' | '.join(options)}")
        return

    as_of, note = resolve_as_of(args.as_of)
    args.as_of = as_of
    if note:
        print(f"note: {note}")

    if args.command == "doctor":
        doctor(args)
        return

    universe, prices, fundamentals = resolve_sources(args)
    provider = build_provider(
        universe, prices, fundamentals, FIXTURES, UNIVERSE_FILE, as_of=as_of
    )
    cfg = ScreenConfig(allow_lookahead=args.ignore_lookahead)
    if args.limit:
        cfg.candidates = args.limit

    print(f"sources: {provider.describe()}\n")

    if args.command == "explain":
        if not args.ticker:
            raise SystemExit("explain needs --ticker")
        explain(provider, args.ticker.upper(), as_of)
        return

    if args.command == "universe":
        tickers = provider.universe(cfg.candidates)
        funnel = getattr(provider.universe_source, "describe_funnel", None)
        if funnel:
            print(funnel(), "\n")
        print(f"{len(tickers)} candidates")
        print(" ".join(tickers))
        return

    if args.command == "screen":
        result = run_screen(provider, args.as_of, cfg)
        print(json.dumps(result.survivors, indent=2) if args.json else result.summary())

        if args.llm:
            assessments = parse_response(call_model(result.survivors))
            violations = validate_grounding(result.survivors, assessments)
            print("\n--- model assessment ---")
            print(json.dumps(assessments, indent=2))
            print("\n--- grounding ---")
            print("clean" if not violations else "\n".join(violations))
            if violations:
                raise SystemExit(1)

    elif args.command == "prompt":
        print(build_prompt(run_screen(provider, args.as_of, cfg).survivors))

    else:
        dates = ["2025-02-14", "2025-05-15", "2025-08-15", "2025-11-14", "2026-02-13"]
        results = backtest(provider, dates, horizon_days=63, cfg=cfg)
        if args.ignore_lookahead:
            print("WARNING: point-in-time guard disabled. Result is contaminated.\n")
        print(report(results))


if __name__ == "__main__":
    main()
