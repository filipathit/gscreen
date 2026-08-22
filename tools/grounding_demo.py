"""Demonstrate the grounding check on two canned model responses.

Response A is disciplined: every figure traces to a supplied field.
Response B is written in the style of the article's published output - it
cites return on equity, PEG and long-term debt, none of which were in the
payload. Under the article's pipeline that text ships as analysis. Here it
fails the build.
"""

from __future__ import annotations

import json
from pathlib import Path

from gscreen.llm import build_payload, parse_response, validate_grounding
from gscreen.providers import FixtureProvider
from gscreen.screen import run_screen

ROOT = Path(__file__).resolve().parent.parent

GROUNDED = json.dumps(
    [
        {
            "ticker": "ALPHA",
            "verdict": "durable",
            "reasoning": "Revenue compounded at 41.8% over three years with 8 consecutive quarters above the growth threshold. A rule-of-40 score of 63.8% means the growth is self-funding rather than bought.",
            "hidden_by_headline": "Nothing material: share count grew only 2.0%, so per-share growth tracks headline growth.",
            "must_stay_true": "FCF margin must hold near 22% as growth decelerates.",
            "evidence": ["revenue_cagr_3y", "consecutive_growth_quarters", "rule_of_40", "share_count_growth", "fcf_margin"],
            "missing_for_confidence": ["customer concentration", "net revenue retention"],
        },
        {
            "ticker": "BRAVO",
            "verdict": "durable",
            "reasoning": "34.6% three-year revenue CAGR with a positive profit margin of 11.0%.",
            "hidden_by_headline": None,
            "must_stay_true": "Margin must not compress as the growth rate steps down.",
            "evidence": ["revenue_cagr_3y", "profit_margin"],
            "missing_for_confidence": ["segment mix"],
        },
    ]
)

UNGROUNDED = json.dumps(
    [
        {
            "ticker": "ALPHA",
            "verdict": "durable",
            "reasoning": "Return on equity sits above 30% with a PEG near 0.8, so the market is not yet pricing in aggressive future growth.",
            "hidden_by_headline": "Long-term debt jumped from roughly 5.5 billion a year ago to over 17 billion now.",
            "must_stay_true": "The 133x trailing P/E leaves no room to disappoint.",
            "evidence": ["return_on_equity", "peg_ratio", "long_term_debt"],
            "missing_for_confidence": [],
        }
    ]
)


def main() -> None:
    provider = FixtureProvider(ROOT / "fixtures")
    facts = run_screen(provider, "2026-08-15").survivors

    print("Fields the model was given:")
    print(json.dumps(build_payload(facts)[0], indent=2)[:600] + "\n")

    for label, raw in (("A: disciplined", GROUNDED), ("B: article-style", UNGROUNDED)):
        violations = validate_grounding(facts, parse_response(raw))
        print(f"--- response {label} ---")
        if violations:
            print(f"REJECTED ({len(violations)} violations)")
            for v in violations:
                print(f"  - {v}")
        else:
            print("PASSED: every figure traces to a supplied field")
        print()


if __name__ == "__main__":
    main()
