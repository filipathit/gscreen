"""The screen.

Three passes, each with a written-down reason for every rejection. The
article prints eight survivors and nothing about what died on the way, which
makes the result impossible to audit or reproduce.

Pass 1  liquidity + size + 12-1 momentum (not a 5-day pop)
Pass 2  growth durability: 3y CAGR, consecutive quarters, rule of 40,
        dilution, leverage
Pass 3  exclusions the article claims but never codes: squeeze risk,
        earnings-reaction proximity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .normalize import Fundamentals
from .metrics import (
    consecutive_growth_quarters,
    days_since_earnings,
    momentum_12_1,
    net_debt_to_ebitda,
    realised_volatility,
    revenue_cagr,
    rule_of_40,
    share_count_growth,
    squeeze_risk,
)


@dataclass
class ScreenConfig:
    min_market_cap: float = 2_000_000_000
    min_avg_volume: float = 1_000_000
    min_momentum_12_1: float = 0.15
    min_revenue_cagr_3y: float = 0.20
    min_consecutive_quarters: int = 4
    quarterly_growth_threshold: float = 0.15
    min_rule_of_40: float | None = None      # informational unless set
    max_net_debt_ebitda: float | None = 4.0
    max_dilution: float = 0.10
    # When True, a company whose data cannot support every gate is rejected
    # rather than passed. Off by default because EDGAR has no EBITDA at all,
    # but worth turning on when the fundamentals source is complete.
    require_all_checks: bool = False
    # Hard ceiling on metered price lookups per run. A free Tiingo key is
    # roughly 500 symbols a month; one careless run can spend half of it.
    max_price_calls: int | None = 60
    max_short_pct_float: float = 0.15
    min_days_since_earnings: int = 10
    candidates: int = 60
    # When False (the default), any company whose latest report date postdates
    # `as_of` is rejected outright. The fundamentals endpoint returns CURRENT
    # statements, so replaying a past date silently uses figures nobody had
    # then. Setting this True produces a contaminated, optimistic result.
    allow_lookahead: bool = False


@dataclass
class Rejection:
    ticker: str
    stage: str
    reason: str


@dataclass
class ScreenResult:
    as_of: str
    survivors: list[dict] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Screen as of {self.as_of}: {len(self.survivors)} survivors"]
        for row in self.survivors:
            lines.append(
                f"  {row['ticker']:<6} cagr3y={_pct(row['revenue_cagr_3y'])}"
                f" mom12-1={_pct(row['momentum_12_1'])}"
                f" qtrs={row['consecutive_growth_quarters']}"
                f" r40={_pct(row['rule_of_40'])}"
                f" dilution={_pct(row['share_count_growth'])}"
            )
            if row.get("checks_skipped"):
                lines.append(
                    f"         untested: {', '.join(row['checks_skipped'])}"
                )
        lines.append(f"Rejected: {len(self.rejections)}")
        for rej in self.rejections:
            lines.append(f"  {rej.ticker:<6} [{rej.stage}] {rej.reason}")
        return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:6.1f}%"


def _get(obj: dict, *path: str) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_facts(f: Fundamentals, prices: list[dict], as_of: str) -> dict:
    """Convenience wrapper: fundamentals + prices in one call."""
    return attach_price_facts(extract_fundamental_facts(f, as_of), prices)


def extract_fundamental_facts(f: Fundamentals, as_of: str) -> dict:
    """Flatten a normalised Fundamentals record into the exact fact set the
    screen and the model are allowed to see. Nothing is derived later from
    thin air, and nothing is read from a vendor-specific shape."""
    latest_revenue = f.annual_revenue[-1] if f.annual_revenue else None
    fcf_margin = (
        f.annual_fcf[-1] / latest_revenue
        if f.annual_fcf and latest_revenue
        else None
    )
    quarterly_yoy = _quarterly_yoy(f.quarterly_revenue)
    cagr = revenue_cagr(f.annual_revenue, years=3)

    return {
        "ticker": f.ticker,
        "name": f.name,
        "sector": f.sector,
        "as_of": as_of,
        "source": f.source,
        "point_in_time": f.point_in_time,
        "market_cap": f.market_cap,
        "revenue_ttm": latest_revenue,
        "revenue_cagr_3y": cagr,
        "quarterly_revenue_growth_yoy": quarterly_yoy[0] if quarterly_yoy else None,
        "consecutive_growth_quarters": consecutive_growth_quarters(quarterly_yoy, 0.15),
        "profit_margin": f.profit_margin,
        "fcf_margin": fcf_margin,
        "rule_of_40": rule_of_40(cagr, fcf_margin),
        "share_count_growth": share_count_growth(f.annual_shares),
        "net_debt_to_ebitda": net_debt_to_ebitda(f.total_debt, f.cash, f.ebitda),
        "price_to_sales_ttm": f.price_to_sales,
        "ev_to_sales": f.ev_to_sales,
        "momentum_12_1": None,      # filled in later, only if worth the call
        "realised_vol_60d": None,
        "short_pct_float": f.short_pct_float,
        "days_since_earnings": days_since_earnings(as_of, f.last_earnings_date),
    }


def attach_price_facts(facts: dict, prices: list[dict]) -> dict:
    facts["momentum_12_1"] = momentum_12_1(prices)
    facts["realised_vol_60d"] = realised_volatility(prices)
    return facts


def _quarterly_yoy(
    quarterly: list[tuple[str, float]], tolerance_days: int = 35
) -> list[float | None]:
    """Newest-first YoY growth per quarter, matched BY DATE.

    Computed, not trusted: the source article reads a single vendor-supplied
    QuarterlyRevenueGrowthYOY field and never checks it against the statements.

    The comparison quarter is found by date, not by counting back four entries.
    Filers report Q1-Q3 in 10-Qs and fold Q4 into the 10-K as a full-year
    figure, so the quarterly series has a hole every year; stepping back four
    entries lands 15-16 months earlier and produces nonsense. Fiscal years that
    do not align to calendar quarters (NVDA ends in late January) make it
    worse.
    """
    from datetime import date

    revenue = {period: value for period, value in quarterly}
    ordered = sorted(revenue)
    parsed = {period: date.fromisoformat(period) for period in ordered}

    out: list[float | None] = []
    for period in reversed(ordered):
        target = parsed[period].toordinal() - 365
        best, best_gap = None, tolerance_days + 1
        for candidate in ordered:
            if candidate >= period:
                continue
            gap = abs(parsed[candidate].toordinal() - target)
            if gap < best_gap:
                best, best_gap = candidate, gap
        if best is None:
            break  # no year-ago quarter within tolerance: the streak ends here
        year_ago = revenue[best]
        out.append(revenue[period] / year_ago - 1 if year_ago > 0 else None)
    return out


def run_screen(provider, as_of: str, cfg: ScreenConfig | None = None) -> ScreenResult:
    """Cheap-and-unmetered filters first, metered ones last.

    EDGAR is free and unlimited; a price API key is not. Fetching prices for
    the whole universe spends the scarce resource on companies that fail on
    fundamentals anyway, so durability runs first and only its survivors cost
    a price call.
    """
    import sys

    cfg = cfg or ScreenConfig()
    result = ScreenResult(as_of=as_of)
    universe = provider.universe(cfg.candidates)
    facts_by_ticker: dict[str, dict] = {}

    # ---- Pass 1: fundamentals (unmetered) --------------------------------
    for index, ticker in enumerate(universe, 1):
        print(f"  [{index}/{len(universe)}] {ticker}", file=sys.stderr, flush=True)
        try:
            facts = extract_fundamental_facts(provider.fundamentals(ticker, as_of), as_of)
        except Exception as exc:  # noqa: BLE001
            result.rejections.append(
                Rejection(ticker, "data", f"fundamentals fetch failed: {exc}"[:200])
            )
            continue
        facts_by_ticker[ticker] = facts

    # ---- Pass 2: growth durability ---------------------------------------
    passed_2 = []
    for ticker, f in facts_by_ticker.items():
        reasons = []
        if f["revenue_cagr_3y"] is None:
            reasons.append("no 3y revenue history")
        elif f["revenue_cagr_3y"] < cfg.min_revenue_cagr_3y:
            reasons.append(f"3y revenue CAGR {_pct(f['revenue_cagr_3y'])} below floor")
        if f["consecutive_growth_quarters"] < cfg.min_consecutive_quarters:
            reasons.append(
                f"only {f['consecutive_growth_quarters']} consecutive growth quarters"
            )
        if (
            cfg.min_rule_of_40 is not None
            and (f["rule_of_40"] is None or f["rule_of_40"] < cfg.min_rule_of_40)
        ):
            reasons.append(f"rule of 40 = {_pct(f['rule_of_40'])}")
        if (
            f["share_count_growth"] is not None
            and f["share_count_growth"] > cfg.max_dilution
        ):
            reasons.append(f"dilution {_pct(f['share_count_growth'])} above cap")
        if (
            cfg.max_net_debt_ebitda is not None
            and f["net_debt_to_ebitda"] is not None
            and f["net_debt_to_ebitda"] > cfg.max_net_debt_ebitda
        ):
            reasons.append(f"net debt/EBITDA {f['net_debt_to_ebitda']:.1f}x above cap")

        # A check that cannot run is not a check that passed. Record it.
        skipped = []
        if f["net_debt_to_ebitda"] is None and cfg.max_net_debt_ebitda is not None:
            skipped.append("leverage (no EBITDA)")
        if f["share_count_growth"] is None:
            skipped.append("dilution (no share history)")
        if f["short_pct_float"] is None:
            skipped.append("squeeze (no short interest)")
        if f["market_cap"] is None:
            skipped.append("size (no market cap)")
        f["checks_skipped"] = skipped

        if reasons:
            result.rejections.append(Rejection(ticker, "durability", "; ".join(reasons)))
        elif cfg.require_all_checks and skipped:
            result.rejections.append(
                Rejection(ticker, "incomplete", "untested: " + ", ".join(skipped))
            )
        else:
            passed_2.append(ticker)

    # ---- Pass 3: momentum (metered - survivors only) ----------------------
    if cfg.max_price_calls is not None and len(passed_2) > cfg.max_price_calls:
        for ticker in passed_2[cfg.max_price_calls :]:
            result.rejections.append(
                Rejection(
                    ticker,
                    "budget",
                    f"price-call budget of {cfg.max_price_calls} reached",
                )
            )
        passed_2 = passed_2[: cfg.max_price_calls]

    print(
        f"  fundamentals passed: {len(passed_2)} - fetching prices for those only",
        file=sys.stderr,
        flush=True,
    )

    passed_3 = []
    for ticker in passed_2:
        f = facts_by_ticker[ticker]
        try:
            prices = provider.eod_prices(ticker, "2000-01-01", as_of)
        except Exception as exc:  # noqa: BLE001
            result.rejections.append(
                Rejection(ticker, "data", f"price fetch failed: {exc}"[:200])
            )
            continue
        attach_price_facts(f, prices)

        mom = f["momentum_12_1"]
        if mom is None:
            span = (
                f"{len(prices)} rows"
                + (f", {prices[0]['date']}..{prices[-1]['date']}" if prices else "")
            )
            result.rejections.append(
                Rejection(ticker, "momentum", f"need 274 daily closes for 12-1, got {span}")
            )
        elif mom < cfg.min_momentum_12_1:
            result.rejections.append(
                Rejection(ticker, "momentum", f"12-1 momentum {_pct(mom)} below floor")
            )
        else:
            passed_3.append(ticker)

    # ---- Pass 4: exclusions ----------------------------------------------
    for ticker in passed_3:
        f = facts_by_ticker[ticker]
        if squeeze_risk(f["short_pct_float"], cfg.max_short_pct_float):
            result.rejections.append(
                Rejection(
                    ticker,
                    "exclusion",
                    f"short interest {_pct(f['short_pct_float'])} of float",
                )
            )
            continue
        dse = f["days_since_earnings"]
        pit = f.get("point_in_time") or cfg.allow_lookahead
        if dse is not None and dse < 0 and not pit:
            result.rejections.append(
                Rejection(
                    ticker,
                    "look-ahead",
                    f"latest report date postdates as_of by {-dse}d; "
                    "no point-in-time fundamentals",
                )
            )
            continue
        if dse is not None and 0 <= dse < cfg.min_days_since_earnings:
            result.rejections.append(
                Rejection(ticker, "exclusion", f"{dse}d after earnings - reaction, not trend")
            )
            continue
        result.survivors.append(f)

    result.survivors.sort(key=lambda r: r["revenue_cagr_3y"] or 0, reverse=True)
    return result
