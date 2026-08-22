"""Command line entry point.

    python -m gscreen.cli screen   --provider fixture
    python -m gscreen.cli screen   --provider eodhd --as-of 2026-08-15 --llm
    python -m gscreen.cli backtest --provider fixture
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backtest import backtest, report
from .llm import build_prompt, call_model, parse_response, validate_grounding
from .providers import EODHDProvider, FixtureProvider
from .screen import ScreenConfig, run_screen

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def get_provider(name: str):
    if name == "fixture":
        return FixtureProvider(FIXTURES)
    return EODHDProvider()


def main() -> None:
    parser = argparse.ArgumentParser(prog="gscreen")
    parser.add_argument("command", choices=["screen", "backtest", "prompt"])
    parser.add_argument("--provider", default="fixture", choices=["fixture", "eodhd"])
    parser.add_argument("--as-of", default="2026-08-15")
    parser.add_argument("--llm", action="store_true", help="call the model and validate grounding")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--ignore-lookahead",
        action="store_true",
        help="disable the point-in-time guard (produces a contaminated backtest)",
    )
    args = parser.parse_args()

    provider = get_provider(args.provider)
    cfg = ScreenConfig(allow_lookahead=args.ignore_lookahead)

    if args.command == "screen":
        result = run_screen(provider, args.as_of, cfg)
        print(json.dumps(result.survivors, indent=2) if args.json else result.summary())

        if args.llm:
            raw = call_model(result.survivors)
            assessments = parse_response(raw)
            violations = validate_grounding(result.survivors, assessments)
            print("\n--- model assessment ---")
            print(json.dumps(assessments, indent=2))
            print("\n--- grounding ---")
            print("clean" if not violations else "\n".join(violations))
            if violations:
                raise SystemExit(1)

    elif args.command == "prompt":
        result = run_screen(provider, args.as_of, cfg)
        print(build_prompt(result.survivors))

    else:
        dates = ["2025-02-14", "2025-05-15", "2025-08-15", "2025-11-14", "2026-02-13"]
        results = backtest(provider, dates, horizon_days=63, cfg=cfg)
        if args.ignore_lookahead:
            print("WARNING: point-in-time guard disabled. Result is contaminated.\n")
        print(report(results))


if __name__ == "__main__":
    main()
