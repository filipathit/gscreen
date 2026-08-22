from __future__ import annotations

import json
from pathlib import Path

import pytest

from gscreen.normalize import from_edgar, from_eodhd
from gscreen.providers import (
    PRESETS,
    CompositeProvider,
    FixtureProvider,
    StaticUniverse,
    build_provider,
    parse_yahoo_chart,
    serialise_filters,
)
from gscreen.screen import ScreenConfig, run_screen

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="module")
def companyfacts():
    return json.loads((FIXTURES / "edgar_companyfacts.json").read_text())


# --------------------------------------------------------------------------
# EDGAR: the point-in-time property is the whole reason it's here
# --------------------------------------------------------------------------


def test_edgar_hides_facts_filed_after_as_of(companyfacts):
    """FY2025 revenue was filed 2026-02-20. On 2026-01-31 it did not exist."""
    early = from_edgar("TEST", companyfacts, as_of="2026-01-31")
    late = from_edgar("TEST", companyfacts, as_of="2026-03-01")
    assert 2_900_000_000 not in early.annual_revenue
    assert 2_900_000_000 in late.annual_revenue
    assert len(late.annual_revenue) == len(early.annual_revenue) + 1


def test_edgar_marks_itself_point_in_time(companyfacts):
    assert from_edgar("TEST", companyfacts, "2026-03-01").point_in_time is True
    raw = json.loads((FIXTURES / "fundamentals.json").read_text())["ALPHA"]
    assert from_eodhd("ALPHA", raw).point_in_time is False


def test_edgar_uses_the_restatement_visible_at_the_time(companyfacts):
    """FY2023 was restated 1.40bn -> 1.38bn, filed 2025-02-14."""
    before = from_edgar("TEST", companyfacts, "2024-06-01")
    after = from_edgar("TEST", companyfacts, "2025-06-01")
    assert 1_400_000_000 in before.annual_revenue
    assert 1_380_000_000 in after.annual_revenue
    assert 1_400_000_000 not in after.annual_revenue


def test_edgar_separates_quarterly_from_annual_by_duration(companyfacts):
    f = from_edgar("TEST", companyfacts, "2026-03-01")
    assert all(v < 1_000_000_000 for _, v in f.quarterly_revenue)
    assert {p for p, _ in f.quarterly_revenue} == {
        "2024-03-31", "2024-06-30", "2025-03-31", "2025-06-30"
    }


def test_edgar_computes_fcf_from_operating_cash_less_capex(companyfacts):
    f = from_edgar("TEST", companyfacts, "2026-03-01")
    assert 400_000_000 in f.annual_fcf  # 500m OCF - 100m capex


def test_edgar_leaves_unavailable_fields_null_rather_than_guessing(companyfacts):
    f = from_edgar("TEST", companyfacts, "2026-03-01")
    assert f.ebitda is None      # not an XBRL concept
    assert f.sector is None      # EDGAR carries SIC, not GICS
    assert f.market_cap is None  # not in EDGAR at all


def test_edgar_market_cap_flows_through_to_price_to_sales(companyfacts):
    f = from_edgar("TEST", companyfacts, "2026-03-01", market_cap=29_000_000_000)
    assert f.price_to_sales == pytest.approx(10.0)


def test_edgar_unknown_ticker_returns_empty_not_exception():
    empty = from_edgar("NOPE", {"facts": {}}, "2026-03-01")
    assert empty.annual_revenue == []
    assert empty.point_in_time is True


# --------------------------------------------------------------------------
# Yahoo
# --------------------------------------------------------------------------


def test_yahoo_parser_drops_null_closes():
    payload = json.loads((FIXTURES / "yahoo_chart.json").read_text())
    rows = parse_yahoo_chart(payload)
    assert len(rows) == 4  # one null hole in a five-day series
    assert all(r["adjusted_close"] > 0 for r in rows)
    assert rows == sorted(rows, key=lambda r: r["date"])


def test_yahoo_parser_handles_empty_result():
    assert parse_yahoo_chart({"chart": {"result": [], "error": "Not Found"}}) == []
    assert parse_yahoo_chart({}) == []


# --------------------------------------------------------------------------
# Universe and composition
# --------------------------------------------------------------------------


def test_static_universe_skips_comments_and_blanks(tmp_path):
    path = tmp_path / "u.txt"
    path.write_text("# a comment\n\nAAA\n  bbb  \n\n# another\nCCC\n")
    assert StaticUniverse(path).universe(limit=10) == ["AAA", "BBB", "CCC"]
    assert StaticUniverse(path).universe(limit=2) == ["AAA", "BBB"]


def test_repo_universe_file_is_parseable():
    tickers = StaticUniverse(ROOT / "universe.txt").universe(limit=1000)
    assert len(tickers) > 20
    assert all(t.isupper() and t.isalpha() for t in tickers)
    assert len(tickers) == len(set(tickers)), "duplicate tickers in universe.txt"


def test_offline_preset_builds_without_credentials():
    provider = build_provider(
        *PRESETS["offline"], fixtures_dir=FIXTURES, universe_file=ROOT / "universe.txt"
    )
    assert isinstance(provider, CompositeProvider)
    assert provider.point_in_time is False
    assert provider.universe(3)


def test_sources_are_independently_swappable():
    """Fixture fundamentals with a static universe - a combination no preset
    defines - must still assemble."""
    provider = build_provider(
        "static", "fixture", "fixture", FIXTURES, ROOT / "universe.txt"
    )
    assert isinstance(provider.universe_source, StaticUniverse)
    assert isinstance(provider.fundamentals_source, FixtureProvider)


def test_every_preset_names_valid_sources():
    from gscreen.providers import SOURCES

    for name, (universe, prices, fundamentals) in PRESETS.items():
        assert universe in SOURCES["universe"], name
        assert prices in SOURCES["prices"], name
        assert fundamentals in SOURCES["fundamentals"], name


def test_free_preset_defaults_to_stooq_not_yahoo():
    """Yahoo throttles shared cloud IPs, which is what CI runs on."""
    assert PRESETS["free"] == ("static", "stooq", "edgar")
    assert PRESETS["free-yahoo"] == ("static", "yahoo", "edgar")
    assert PRESETS["paid"] == ("eodhd", "eodhd", "eodhd")


# --------------------------------------------------------------------------
# Point-in-time changes screen behaviour
# --------------------------------------------------------------------------


def test_lookahead_guard_still_fires_for_snapshot_sources():
    provider = FixtureProvider(FIXTURES)
    result = run_screen(provider, "2025-02-14", ScreenConfig())
    assert any(r.stage == "look-ahead" for r in result.rejections)


def test_serialise_filters_is_json():
    assert json.loads(serialise_filters([["a", "=", "b"]])) == [["a", "=", "b"]]


def test_a_failing_ticker_is_logged_not_fatal():
    """Yahoo throttles. One 429 must not end the run."""

    class Flaky(FixtureProvider):
        def eod_prices(self, ticker, start, end):
            if ticker == "ALPHA":
                raise RuntimeError("429 Too Many Requests")
            return super().eod_prices(ticker, start, end)

    result = run_screen(Flaky(FIXTURES), "2026-08-15", ScreenConfig())
    failed = [r for r in result.rejections if r.stage == "data"]
    assert [r.ticker for r in failed] == ["ALPHA"]
    assert "BRAVO" in [row["ticker"] for row in result.survivors]


# --------------------------------------------------------------------------
# Stooq + error reporting
# --------------------------------------------------------------------------


def test_stooq_csv_parses_and_filters_by_date():
    from gscreen.providers import parse_stooq_csv

    csv = (
        "Date,Open,High,Low,Close,Volume\n"
        "2024-12-31,9,10,8,9.5,100\n"
        "2025-01-02,10,11,9,10.5,1000\n"
        "2025-01-03,10.5,12,10,11.25,900\n"
    )
    rows = parse_stooq_csv(csv, "2025-01-01", "2025-12-31")
    assert [r["date"] for r in rows] == ["2025-01-02", "2025-01-03"]
    assert rows[1]["adjusted_close"] == 11.25


def test_stooq_handles_error_bodies_and_gaps():
    from gscreen.providers import parse_stooq_csv

    from gscreen.providers import FetchError

    # A refusal must surface as a fetch failure, not as "no data".
    with pytest.raises(FetchError):
        parse_stooq_csv("Exceeded the daily hits limit", "2020-01-01", "2030-01-01")
    assert parse_stooq_csv("", "2020-01-01", "2030-01-01") == []
    gappy = "Date,Close\n2025-01-02,\n2025-01-03,N/A\n2025-01-06,12.0\n"
    assert len(parse_stooq_csv(gappy, "2020-01-01", "2030-01-01")) == 1


def test_fetch_error_reports_status_and_host():
    from gscreen.providers import FetchError

    err = FetchError("https://example.com/api?token=secret", 429, "throttled")
    text = str(err)
    assert "429" in text and "throttled" in text
    assert "token=secret" not in text, "query string must not leak into the message"


def test_data_rejection_carries_the_real_reason():
    """The first free run rejected ten tickers with a bare RuntimeError, which
    was useless. The reason must now name the failure."""
    from gscreen.providers import FetchError

    class Broken(FixtureProvider):
        def eod_prices(self, ticker, start, end):
            raise FetchError("https://prices.example/x", 429, "throttled")

    result = run_screen(Broken(FIXTURES), "2026-08-15", ScreenConfig())
    reasons = [r.reason for r in result.rejections if r.stage == "data"]
    assert reasons and all("429" in r and "throttled" in r for r in reasons)


def test_short_history_rejection_states_what_it_got():
    """'insufficient price history' with no numbers is not a diagnosis."""

    class Short(FixtureProvider):
        def eod_prices(self, ticker, start, end):
            return super().eod_prices(ticker, start, end)[:30]

    result = run_screen(Short(FIXTURES), "2026-08-15", ScreenConfig())
    reasons = [r.reason for r in result.rejections if r.stage == "momentum"]
    assert reasons
    assert all("30 rows" in r and "274" in r for r in reasons)
