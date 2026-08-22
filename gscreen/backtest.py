"""Backtest harness.

The article presents one snapshot and no evidence the screen works. A screen
without a forward test is a story about the past. This reruns the screen at
past dates and compares equal-weighted survivor returns against the
equal-weighted universe over the same window.

Caveats that matter and are easy to forget:
  - The fundamentals endpoint returns CURRENT statements. Replaying it at a
    past date leaks information that was not public then. For a defensible
    backtest you need point-in-time data with the original report dates;
    treat this harness as scaffolding, and its output as optimistic.
  - No survivorship correction: delisted names are absent from the screener,
    which flatters any result.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median

from .dates import previous_trading_day
from .metrics import forward_return
from .screen import ScreenConfig, run_screen


@dataclass
class PeriodResult:
    as_of: str
    n_survivors: int
    survivor_return: float | None
    benchmark_return: float | None
    n_lookahead: int = 0

    @property
    def excess(self) -> float | None:
        if self.survivor_return is None or self.benchmark_return is None:
            return None
        return self.survivor_return - self.benchmark_return


def backtest(
    provider,
    dates: list[str],
    horizon_days: int = 63,
    cfg: ScreenConfig | None = None,
    universe: list[str] | None = None,
) -> list[PeriodResult]:
    cfg = cfg or ScreenConfig()
    results = []

    if not getattr(provider, "universe_point_in_time", True):
        print(
            "WARNING: the universe source is not point-in-time. Candidates were\n"
            "         selected using figures published after the rebalance date,\n"
            "         which flatters every number below. Use a static universe\n"
            "         fixed in advance for a defensible backtest.\n"
        )

    # A rebalance date on a weekend silently shifts every window.
    dates = [previous_trading_day(d) for d in dates]

    for as_of in dates:
        screen = run_screen(provider, as_of, cfg)
        tickers = [row["ticker"] for row in screen.survivors]

        bench_tickers = universe or sorted(
            {row["ticker"] for row in screen.survivors}
            | {rej.ticker for rej in screen.rejections}
        )

        survivor_rets = _returns(provider, tickers, as_of, horizon_days)
        bench_rets = _returns(provider, bench_tickers, as_of, horizon_days)

        results.append(
            PeriodResult(
                as_of=as_of,
                n_survivors=len(tickers),
                survivor_return=mean(survivor_rets) if survivor_rets else None,
                benchmark_return=mean(bench_rets) if bench_rets else None,
                n_lookahead=sum(
                    1 for r in screen.rejections if r.stage == "look-ahead"
                ),
            )
        )
    return results


def _returns(provider, tickers: list[str], as_of: str, horizon: int) -> list[float]:
    out = []
    for ticker in tickers:
        # No ".US" suffix: each provider handles its own symbol format now.
        prices = provider.eod_prices(ticker, "2000-01-01", "2100-01-01")
        ret = forward_return(prices, as_of, horizon)
        if ret is not None:
            out.append(ret)
    return out


def report(results: list[PeriodResult]) -> str:
    lines = [
        f"{'date':<12}{'n':>4}{'survivors':>12}{'benchmark':>12}{'excess':>10}{'look-ahead':>12}",
        "-" * 62,
    ]
    excesses = []
    blocked = 0
    for r in results:
        lines.append(
            f"{r.as_of:<12}{r.n_survivors:>4}"
            f"{_fmt(r.survivor_return):>12}{_fmt(r.benchmark_return):>12}"
            f"{_fmt(r.excess):>10}{r.n_lookahead:>12}"
        )
        if r.excess is not None:
            excesses.append(r.excess)
        blocked += r.n_lookahead

    if blocked and not excesses:
        lines += [
            "-" * 62,
            f"{blocked} names blocked as look-ahead across {len(results)} periods.",
            "",
            "This is the honest result, not a bug. The fundamentals endpoint",
            "serves CURRENT statements, so a past-dated rerun would score the",
            "screen on figures that were not public at the time. The article",
            "publishes no backtest at all, which conceals the same problem",
            "rather than solving it. To backtest this properly you need",
            "point-in-time fundamentals with original filing dates.",
            "Rerun with --ignore-lookahead to see the contaminated version.",
        ]
    if excesses:
        wins = sum(1 for e in excesses if e > 0)
        lines += [
            "-" * 50,
            f"periods={len(excesses)}  mean excess={mean(excesses) * 100:.2f}%"
            f"  median={median(excesses) * 100:.2f}%  hit rate={wins}/{len(excesses)}",
            "",
            "Sample this small proves nothing. It is a harness, not a result.",
        ]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"
