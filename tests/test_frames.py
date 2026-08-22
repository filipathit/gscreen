from __future__ import annotations

import pytest

from gscreen.frames import FramesConfig, FramesUniverse


class FakeFrames(FramesUniverse):
    """FramesUniverse with the network swapped for a dict.

    revenue[cik][year] and a cik -> ticker map, so the funnel logic can be
    tested exactly without touching data.sec.gov.
    """

    def __init__(self, revenue, tickers, as_of="2026-08-21", cfg=None):
        self._revenue = revenue
        self._fake_tickers = tickers
        self.as_of = as_of
        self.cfg = cfg or FramesConfig()
        self.report = {}
        self._tickers = None

    def _frame(self, tag, period):
        if tag != self.cfg.tags[0]:
            return {}  # only the first tag reports, unless a test says otherwise
        year = int(period[2:6])
        return {
            cik: series[year]
            for cik, series in self._revenue.items()
            if year in series
        }

    def ticker_for(self, cik):
        return self._fake_tickers.get(cik)


def build(**overrides):
    revenue = {
        # 3y CAGR ~26%, large enough  -> keep
        111: {2022: 1_000_000_000, 2023: 1_300_000_000, 2024: 1_600_000_000, 2025: 2_000_000_000},
        # ~3% CAGR -> too slow
        222: {2022: 1_000_000_000, 2023: 1_030_000_000, 2024: 1_060_000_000, 2025: 1_090_000_000},
        # fast but tiny -> below revenue floor
        333: {2022: 1_000_000, 2023: 2_000_000, 2024: 4_000_000, 2025: 8_000_000},
        # fast and large but no ticker -> private filer
        444: {2022: 1_000_000_000, 2023: 1_500_000_000, 2024: 2_200_000_000, 2025: 3_000_000_000},
        # missing the base year -> incomplete history
        555: {2024: 1_000_000_000, 2025: 2_000_000_000},
    }
    tickers = {111: "FAST", 222: "SLOW", 333: "TINY", 555: "NEWCO"}
    return FakeFrames(revenue, tickers, **overrides)


# --------------------------------------------------------------------------
# Funnel
# --------------------------------------------------------------------------


def test_universe_keeps_only_large_fast_mapped_filers():
    assert build().universe() == ["FAST"]


def test_funnel_counts_every_rejection_reason():
    frames = build()
    frames.universe()
    report = frames.report
    assert report["filers_with_revenue"] == 5
    assert report["rejected_slow_growth"] == 1       # SLOW
    assert report["rejected_too_small"] == 1         # TINY
    assert report["rejected_no_ticker"] == 1         # cik 444
    assert report["rejected_incomplete_history"] == 1  # NEWCO
    assert report["candidates"] == 1


def test_funnel_is_printable_and_mentions_the_period():
    frames = build()
    frames.universe()
    text = frames.describe_funnel()
    assert "CY2025" in text and "rejected_slow_growth" in text


def test_results_are_ranked_by_growth():
    revenue = {
        1: {2022: 1e9, 2023: 1.2e9, 2024: 1.4e9, 2025: 1.7e9},   # ~19%... 
        2: {2022: 1e9, 2023: 2e9, 2024: 4e9, 2025: 8e9},          # 100%
        3: {2022: 1e9, 2023: 1.5e9, 2024: 2.2e9, 2025: 3.4e9},    # ~50%
    }
    frames = FakeFrames(revenue, {1: "A", 2: "B", 3: "C"},
                        cfg=FramesConfig(min_cagr=0.10))
    assert frames.universe() == ["B", "C", "A"]


def test_limit_caps_the_candidate_list():
    revenue = {i: {2022: 1e9, 2023: 2e9, 2024: 3e9, 2025: 4e9} for i in range(1, 11)}
    tickers = {i: f"TCK{chr(64 + i)}" for i in range(1, 11)}
    frames = FakeFrames(revenue, tickers)
    assert len(frames.universe(limit=3)) == 3
    assert len(frames.universe()) == 10


# --------------------------------------------------------------------------
# Period selection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "as_of,expected",
    [
        ("2026-08-21", 2025),  # after April: CY2025 is framed
        ("2026-04-01", 2025),
        ("2026-02-15", 2024),  # before April: CY2025 not yet filed
        ("2026-01-02", 2024),
    ],
)
def test_latest_complete_year_waits_for_filing_season(as_of, expected):
    assert build(as_of=as_of).latest_complete_year() == expected


def test_cagr_window_uses_years_plus_one_frames():
    frames = build()
    frames.universe()
    # 2025 back to 2022 inclusive is four annual frames for a 3y CAGR
    assert frames.cfg.years == 3
    assert frames.latest_complete_year() == 2025


# --------------------------------------------------------------------------
# Tag merging
# --------------------------------------------------------------------------


def test_second_tag_fills_gaps_left_by_the_first():
    """Filers use different revenue tags; a company reported under the second
    tag must not be dropped."""

    class TwoTags(FakeFrames):
        def _frame(self, tag, period):
            year = int(period[2:6])
            if tag == self.cfg.tags[0]:
                return {111: self._revenue[111][year]}
            if tag == self.cfg.tags[1]:
                return {999: 1_000_000_000 * (1.4 ** (year - 2022))}
            return {}

    frames = TwoTags(
        {111: {2022: 1e9, 2023: 1.3e9, 2024: 1.6e9, 2025: 2e9}},
        {111: "FAST", 999: "OTHERTAG"},
    )
    assert set(frames.universe()) == {"FAST", "OTHERTAG"}


def test_first_tag_wins_when_both_report_the_same_company():
    class Conflicting(FakeFrames):
        def _frame(self, tag, period):
            year = int(period[2:6])
            if tag == self.cfg.tags[0]:
                return {111: self._revenue[111][year]}
            return {111: 1.0}  # nonsense from the fallback tag

        def revenue_check(self):
            return self.revenue_by_year()[111]

    frames = Conflicting(
        {111: {2022: 1e9, 2023: 1.3e9, 2024: 1.6e9, 2025: 2e9}}, {111: "FAST"}
    )
    assert frames.revenue_check()[2025] == 2e9


# --------------------------------------------------------------------------
# The look-ahead property must be advertised
# --------------------------------------------------------------------------


def test_frames_declare_themselves_not_point_in_time():
    assert FramesUniverse.point_in_time is False


def test_backtest_warns_when_the_universe_leaks(capsys):
    from pathlib import Path

    from gscreen.backtest import backtest
    from gscreen.providers import CompositeProvider, FixtureProvider

    fixture = FixtureProvider(Path(__file__).resolve().parent.parent / "fixtures")

    class LeakyUniverse:
        point_in_time = False

        def universe(self, limit):
            return fixture.universe(limit)

    provider = CompositeProvider(LeakyUniverse(), fixture, fixture)
    assert provider.universe_point_in_time is False
    backtest(provider, ["2026-02-13"], horizon_days=5)
    assert "not point-in-time" in capsys.readouterr().out


def test_static_universe_does_not_trigger_the_warning(capsys, tmp_path):
    from pathlib import Path

    from gscreen.backtest import backtest
    from gscreen.providers import CompositeProvider, FixtureProvider, StaticUniverse

    root = Path(__file__).resolve().parent.parent
    fixture = FixtureProvider(root / "fixtures")
    listing = tmp_path / "u.txt"
    listing.write_text("ALPHA\nBRAVO\n")
    static = StaticUniverse(listing)
    static.point_in_time = True  # a list fixed in advance leaks nothing
    provider = CompositeProvider(static, fixture, fixture)
    backtest(provider, ["2026-02-13"], horizon_days=5)
    assert "not point-in-time" not in capsys.readouterr().out


def test_backtest_does_not_mangle_symbols():
    """A leftover ".US" suffix from the EODHD-only days broke every non-EODHD
    price lookup in the backtest path."""
    from pathlib import Path

    from gscreen.backtest import _returns
    from gscreen.providers import FixtureProvider

    fixture = FixtureProvider(Path(__file__).resolve().parent.parent / "fixtures")
    seen = []

    class Recording(FixtureProvider):
        def eod_prices(self, ticker, start, end):
            seen.append(ticker)
            return super().eod_prices(ticker, start, end)

    _returns(Recording(Path(__file__).resolve().parent.parent / "fixtures"),
             ["ALPHA"], "2026-02-13", 5)
    assert seen == ["ALPHA"]


# --------------------------------------------------------------------------
# Share-class filtering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,keep",
    [
        ("NVDA", True), ("ALAB", True), ("A", True),
        ("ANG-PD", False),   # preferred series
        ("ATH-PA", False),   # preferred series
        ("BRK.B", False),    # dual class suffix
        ("VREOF", False),    # OTC foreign ordinary
        ("JETMF", False),
        ("LKNCY", False),    # ADR
        ("GOOGL", True),     # five letters but not F/Y
    ],
)
def test_only_common_stock_survives(ticker, keep):
    from gscreen.frames import is_common_stock

    assert is_common_stock(ticker) is keep


def test_non_common_tickers_are_counted_in_the_funnel():
    revenue = {
        1: {2022: 1e9, 2023: 2e9, 2024: 3e9, 2025: 4e9},
        2: {2022: 1e9, 2023: 2e9, 2024: 3e9, 2025: 4e9},
    }
    frames = FakeFrames(revenue, {1: "GOOD", 2: "ANG-PD"})
    assert frames.universe() == ["GOOD"]
    assert frames.report["rejected_not_common_stock"] == 1
