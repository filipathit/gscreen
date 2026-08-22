"""The judgment layer - and the check on it.

The article's own thesis is "the model reasons over numbers it can see, it
doesn't invent them." Its published output then cites ROE, PEG, operating
margin, long-term debt and trailing P/E, none of which appear in the dict its
code builds. So the thesis is asserted, never enforced.

This module enforces it:
  1. FACT_FIELDS is the only thing the model is shown.
  2. The model must return JSON and cite, per claim, the field names it used.
  3. validate_grounding() rejects cited fields that weren't supplied, and
     flags numeric literals in the prose that trace to no supplied value.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# The model sees these and nothing else.
FACT_FIELDS = [
    "ticker",
    "name",
    "sector",
    "as_of",
    "market_cap",
    "revenue_ttm",
    "revenue_cagr_3y",
    "quarterly_revenue_growth_yoy",
    "consecutive_growth_quarters",
    "profit_margin",
    "fcf_margin",
    "rule_of_40",
    "share_count_growth",
    "net_debt_to_ebitda",
    "price_to_sales_ttm",
    "ev_to_sales",
    "momentum_12_1",
    "realised_vol_60d",
    "short_pct_float",
    "days_since_earnings",
]

PROMPT_TEMPLATE = """You are assessing growth durability for a research screen.

You may use ONLY the JSON facts below. You have no other information about
these companies. If a judgement would require a figure that is not present
(return on equity, PEG, P/E, debt levels, customer concentration, anything
else), you must not state it. Say the data is insufficient instead.

FACTS:
{facts}

FIELD NOTES:
- revenue_cagr_3y, quarterly_revenue_growth_yoy, margins, share_count_growth,
  short_pct_float and momentum_12_1 are decimal fractions (0.31 = 31%).
- rule_of_40 is revenue_cagr_3y + fcf_margin.
- A null means the value was unavailable, not zero.

Return ONLY a JSON array, no prose or code fences. One object per ticker:
{{
  "ticker": "<ticker>",
  "verdict": "durable" | "unproven" | "fragile" | "insufficient_data",
  "reasoning": "<two sentences max, every figure drawn from FACTS>",
  "hidden_by_headline": "<what the headline growth number conceals, or null>",
  "must_stay_true": "<one line>",
  "evidence": ["<field names from FACTS you relied on>"],
  "missing_for_confidence": ["<fields you would need but were not given>"]
}}

Rank by growth quality. Do not recommend buying or selling anything.
"""


def build_payload(facts: list[dict]) -> list[dict]:
    """Strip every fact row down to the whitelist."""
    return [{k: row.get(k) for k in FACT_FIELDS} for row in facts]


def build_prompt(facts: list[dict]) -> str:
    return PROMPT_TEMPLATE.format(facts=json.dumps(build_payload(facts), indent=2))


def call_model(facts: list[dict], model: str = "claude-sonnet-5") -> str:
    """Live call. Requires ANTHROPIC_API_KEY.

    Note the model string: the article pins claude-sonnet-4-6, a generation
    behind. Pin deliberately and revisit it; do not inherit it from a blog post.
    """
    import anthropic  # imported lazily so the offline path needs no SDK

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0,
        messages=[{"role": "user", "content": build_prompt(facts)}],
    )
    # Concatenate text blocks rather than assuming content[0] is text.
    return "".join(b.text for b in response.content if b.type == "text")


# --------------------------------------------------------------------------
# Grounding validation
# --------------------------------------------------------------------------

# The lookbehind stops hyphenated words ("rule-of-40", "12-1") being read as
# negative numbers.
_NUMBER = re.compile(
    r"(?<![\w.\-])-?\$?\d[\d,]*\.?\d*\s*(?:%|x|bn|b|m)?", re.IGNORECASE
)


def parse_response(raw: str) -> list[dict]:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("Expected a JSON array of per-ticker objects.")
    return parsed


def _candidate_values(row: dict) -> list[float]:
    """Every way a supplied value might legitimately be written out."""
    out: list[float] = []
    for value in row.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            v = float(value)
            out += [v, v * 100, v / 100, abs(v)]
            if abs(v) > 1e6:
                out += [v / 1e6, v / 1e9]
    return out


def _numbers_in(text: str) -> list[float]:
    found = []
    for match in _NUMBER.finditer(text or ""):
        token = match.group().strip()
        suffix = token[-1].lower() if token and token[-1].isalpha() else ""
        digits = re.sub(r"[^\d.\-]", "", token)
        if digits in ("", "-", "."):
            continue
        try:
            value = float(digits)
        except ValueError:
            continue
        if suffix in ("b", "n"):
            value *= 1
        found.append(value)
    return found


def validate_grounding(
    facts: list[dict], assessments: list[dict], tolerance: float = 0.02
) -> list[str]:
    """Return a list of grounding violations. Empty list means clean."""
    payload = {row["ticker"]: row for row in build_payload(facts)}
    violations: list[str] = []

    for item in assessments:
        ticker = item.get("ticker")
        row = payload.get(ticker)
        if row is None:
            violations.append(f"{ticker}: not in the supplied fact set")
            continue

        supplied = {k for k, v in row.items() if v is not None}
        for field in item.get("evidence", []):
            if field not in FACT_FIELDS:
                violations.append(f"{ticker}: cites unknown field '{field}'")
            elif field not in supplied:
                violations.append(
                    f"{ticker}: cites '{field}' which was null in the payload"
                )

        allowed = _candidate_values(row) + [
            float(item.get("consecutive_growth_quarters") or 0)
        ]
        text = " ".join(
            str(item.get(k) or "")
            for k in ("reasoning", "hidden_by_headline", "must_stay_true")
        )
        for number in _numbers_in(text):
            if number in (0.0, 1.0, 2.0, 3.0, 4.0, 40.0):  # ordinals / rule-of-40 name
                continue
            if not any(
                abs(number - cand) <= max(tolerance, abs(cand) * tolerance)
                for cand in allowed
            ):
                violations.append(
                    f"{ticker}: figure {number:g} in the prose traces to no supplied field"
                )
    return violations
