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
    check(
        "prices (stooq)",
        lambda: f"{len(StooqPrices().eod_prices(ticker, '2024-01-01', as_of))} rows",
    )
    check(
        "prices (yahoo)",
        lambda: f"{len(YahooPrices().eod_prices(ticker, '2024-01-01', as_of))} rows",
    )

    edgar = EdgarFundamentals()
    check("edgar ticker index", lambda: f"CIK {edgar.cik_for(ticker)}")
    check(
        "edgar fundamentals",
        lambda: (
            f"{len(edgar.fundamentals(ticker, as_of).annual_revenue)} annual "
            "revenue periods"
        ),
    )
    print(
        "\nA 403 from EDGAR usually means SEC_USER_AGENT is unset or generic."
        "\nA 429 from Yahoo is throttling - use --prices stooq instead."
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="gscreen")
    parser.add_argument(
        "command", choices=["screen", "backtest", "prompt", "sources", "doctor"]
    )
    parser.add_argument("--preset", default="offline", choices=sorted(PRESETS))
    parser.add_argument("--universe", choices=SOURCES["universe"])
    parser.add_argument("--prices", choices=SOURCES["prices"])
    parser.add_argument("--fundamentals", choices=SOURCES["fundamentals"])
    parser.add_argument("--as-of", default="2026-08-15")
    parser.add_argument("--limit", type=int, default=None, help="cap the universe size")
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

    if args.command == "doctor":
        doctor(args)
        return

    universe, prices, fundamentals = resolve_sources(args)
    provider = build_provider(universe, prices, fundamentals, FIXTURES, UNIVERSE_FILE)
    cfg = ScreenConfig(allow_lookahead=args.ignore_lookahead)
    if args.limit:
        cfg.candidates = args.limit

    print(f"sources: {provider.describe()}\n")

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
