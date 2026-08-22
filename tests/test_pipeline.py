from __future__ import annotations

import json
from pathlib import Path

import pytest

from gscreen import metrics
from gscreen.llm import parse_response, validate_grounding
from gscreen.providers import FixtureProvider, serialise_filters
from gscreen.screen import ScreenConfig, extract_facts, run_screen

ROOT = Path(__file__).resolve().parent.parent
AS_OF = "2026-08-15"


@pytest.fixture(scope="module")
def provider():
    return FixtureProvider(ROOT / "fixtures")


@pytest.fixture(scope="module")
def screened(provider):
    return run_screen(provider, AS_OF, ScreenConfig())


# --------------------------------------------------------------------------
# The bug the article ships
# --------------------------------------------------------------------------


def test_filters_serialise_to_valid_json():
    filters = [["exchange", "=", "us"], ["avgvol_200d", ">", 1_000_000]]
    encoded = serialise_filters(filters)
    assert "'" not in encoded
    assert json.loads(encoded) == filters


def test_str_of_list_is_not_valid_json():
    """Guards the regression: str() is what the article uses."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(str([["exchange", "=", "us"]]))


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def test_revenue_cagr():
    assert metrics.revenue_cagr([100, 200, 400, 800], years=3) == pytest.approx(1.0)
    assert metrics.revenue_cagr([100, 200], years=3) is None
    assert metrics.revenue_cagr([0, 1, 2, 3], years=3) is None


def test_consecutive_growth_quarters_breaks_on_first_miss():
    assert metrics.consecutive_growth_quarters([0.3, 0.25, 0.05, 0.9], 0.15) == 2
    assert metrics.consecutive_growth_quarters([0.3, None, 0.4], 0.15) == 1


def test_rule_of_40_and_dilution():
    assert metrics.rule_of_40(0.30, 0.15) == pytest.approx(0.45)
    assert metrics.rule_of_40(0.30, None) is None
    assert metrics.share_count_growth([100, 119]) == pytest.approx(0.19)


def test_net_debt_to_ebitda_handles_net_cash():
    assert metrics.net_debt_to_ebitda(1e9, 3e9, 1e9) == pytest.approx(-2.0)
    assert metrics.net_debt_to_ebitda(1e9, 3e9, 0) is None


def test_momentum_skips_the_most_recent_month():
    """A late spike must not enter the 12-1 figure - the whole point of the skip."""
    flat = [{"date": f"d{i}", "adjusted_close": 100.0} for i in range(300)]
    spiked = [dict(row) for row in flat]
    for row in spiked[-10:]:
        row["adjusted_close"] = 200.0
    assert metrics.momentum_12_1(flat) == pytest.approx(0.0)
    assert metrics.momentum_12_1(spiked) == pytest.approx(0.0)


def test_momentum_requires_enough_history():
    assert metrics.momentum_12_1([{"adjusted_close": 1.0}] * 50) is None


def test_squeeze_and_earnings_helpers():
    assert metrics.squeeze_risk(0.24) is True
    assert metrics.squeeze_risk(0.03) is False
    assert metrics.squeeze_risk(None) is None
    assert metrics.days_since_earnings("2026-08-15", "2026-08-11") == 4


# --------------------------------------------------------------------------
# Screen behaviour - one test per rejection branch
# --------------------------------------------------------------------------


def _reason(result, ticker):
    return next(r for r in result.rejections if r.ticker == ticker)


def test_survivors_are_the_durable_names(screened):
    assert [row["ticker"] for row in screened.survivors] == ["ALPHA", "BRAVO"]


@pytest.mark.parametrize(
    "ticker,stage,fragment",
    [
        ("INDIA", "momentum", "12-1 momentum"),
        ("CHARL", "durability", "net debt/EBITDA"),
        ("HOTEL", "durability", "CAGR"),
        ("DELTA", "durability", "consecutive growth quarters"),
        ("ECHO", "durability", "dilution"),
        ("GOLF", "exclusion", "after earnings"),
        ("FOXTR", "exclusion", "short interest"),
    ],
)
def test_each_rejection_branch_fires(screened, ticker, stage, fragment):
    rejection = _reason(screened, ticker)
    assert rejection.stage == stage
    assert fragment in rejection.reason


def test_one_quarter_wonder_is_excluded(screened, provider):
    """DELTA's latest quarter is +44% YoY - the exact number the article would
    screen on - yet it has no streak behind it and is correctly rejected."""
    facts = extract_facts(
        provider.fundamentals("DELTA", "2026-08-15"),
        provider.eod_prices("DELTA", "2000-01-01", "2026-08-15"),
        "2026-08-15",
    )
    assert facts["quarterly_revenue_growth_yoy"] > 0.40
    assert facts["consecutive_growth_quarters"] < 4
    assert "DELTA" not in [row["ticker"] for row in screened.survivors]


def test_every_rejection_has_a_written_reason(screened):
    assert all(r.reason for r in screened.rejections)


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------


def test_grounding_rejects_unsupplied_fields(screened):
    response = json.dumps(
        [
            {
                "ticker": "ALPHA",
                "verdict": "durable",
                "reasoning": "Return on equity above 30% with a PEG near 0.8.",
                "hidden_by_headline": None,
                "must_stay_true": "x",
                "evidence": ["return_on_equity", "peg_ratio"],
                "missing_for_confidence": [],
            }
        ]
    )
    violations = validate_grounding(screened.survivors, parse_response(response))
    assert any("return_on_equity" in v for v in violations)
    assert any("30" in v for v in violations)


def test_grounding_accepts_supplied_figures(screened):
    row = screened.survivors[0]
    response = json.dumps(
        [
            {
                "ticker": row["ticker"],
                "verdict": "durable",
                "reasoning": f"Three-year revenue CAGR of {row['revenue_cagr_3y'] * 100:.1f}%.",
                "hidden_by_headline": None,
                "must_stay_true": "Margins hold.",
                "evidence": ["revenue_cagr_3y"],
                "missing_for_confidence": [],
            }
        ]
    )
    assert validate_grounding(screened.survivors, parse_response(response)) == []


def test_grounding_rejects_unknown_ticker(screened):
    response = json.dumps([{"ticker": "ZULU", "evidence": [], "reasoning": ""}])
    violations = validate_grounding(screened.survivors, parse_response(response))
    assert any("not in the supplied fact set" in v for v in violations)


def test_parse_response_strips_code_fences(screened):
    assert parse_response('```json\n[{"ticker": "A"}]\n```') == [{"ticker": "A"}]
