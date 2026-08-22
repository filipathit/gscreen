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


def _sorted_yearly(block: dict | None, key: str) -> list[float]:
    """EODHD financial statements are dicts keyed by date. Oldest first."""
    if not block:
        return []
    out = []
    for period in sorted(block):
        raw = block[period].get(key)
        if raw not in (None, "", "None"):
            out.append(float(raw))
    return out


def extract_facts(ticker: str, fundamentals: dict, prices: list[dict], as_of: str) -> dict:
    """Flatten EODHD fundamentals into the exact fact set the screen and the
    model are allowed to see. Nothing is derived later from thin air."""
    income_y = _get(fundamentals, "Financials", "Income_Statement", "yearly") or {}
    income_q = _get(fundamentals, "Financials", "Income_Statement", "quarterly") or {}
    balance_y = _get(fundamentals, "Financials", "Balance_Sheet", "yearly") or {}
    cash_y = _get(fundamentals, "Financials", "Cash_Flow", "yearly") or {}

    revenues = _sorted_yearly(income_y, "totalRevenue")
    shares = _sorted_yearly(balance_y, "commonStockSharesOutstanding")
    fcf = _sorted_yearly(cash_y, "freeCashFlow")

    latest_revenue = revenues[-1] if revenues else None
    fcf_margin = (
        fcf[-1] / latest_revenue if fcf and latest_revenue else None
    )

    quarterly_yoy = _quarterly_yoy(income_q)
    cagr = revenue_cagr(revenues, years=3)

    total_debt = _sorted_yearly(balance_y, "shortLongTermDebtTotal")
    cash_eq = _sorted_yearly(balance_y, "cashAndShortTermInvestments")
    ebitda = _get(fundamentals, "Highlights", "EBITDA")

    return {
        "ticker": ticker,
        "name": _get(fundamentals, "General", "Name"),
        "sector": _get(fundamentals, "General", "Sector"),
        "as_of": as_of,
        "market_cap": _get(fundamentals, "Highlights", "MarketCapitalization"),
        "revenue_ttm": latest_revenue,
        "revenue_cagr_3y": cagr,
        "quarterly_revenue_growth_yoy": quarterly_yoy[0] if quarterly_yoy else None,
        "consecutive_growth_quarters": consecutive_growth_quarters(quarterly_yoy, 0.15),
        "profit_margin": _get(fundamentals, "Highlights", "ProfitMargin"),
        "fcf_margin": fcf_margin,
        "rule_of_40": rule_of_40(cagr, fcf_margin),
        "share_count_growth": share_count_growth(shares),
        "net_debt_to_ebitda": net_debt_to_ebitda(
            total_debt[-1] if total_debt else None,
            cash_eq[-1] if cash_eq else None,
            ebitda,
        ),
        "price_to_sales_ttm": _get(fundamentals, "Valuation", "PriceSalesTTM"),
        "ev_to_sales": _get(fundamentals, "Valuation", "EnterpriseValueRevenue"),
        "momentum_12_1": momentum_12_1(prices),
        "realised_vol_60d": realised_volatility(prices),
        "short_pct_float": _get(fundamentals, "SharesStats", "ShortPercentFloat"),
        "days_since_earnings": days_since_earnings(
            as_of, _get(fundamentals, "Earnings", "Last_Reported_Date")
        ),
    }


def _quarterly_yoy(income_q: dict) -> list[float | None]:
    """Newest-first YoY growth per quarter, computed from raw revenue.

    Computed, not trusted: the article reads a single vendor-supplied
    QuarterlyRevenueGrowthYOY field and never checks it against the statements.
    """
    periods = sorted(income_q)
    revenue = {}
    for period in periods:
        raw = income_q[period].get("totalRevenue")
        if raw not in (None, "", "None"):
            revenue[period] = float(raw)
    ordered = sorted(revenue)
    out: list[float | None] = []
    for i in range(len(ordered) - 1, 3, -1):
        now, year_ago = revenue[ordered[i]], revenue[ordered[i - 4]]
        out.append(now / year_ago - 1 if year_ago > 0 else None)
    return out


def run_screen(provider, as_of: str, cfg: ScreenConfig | None = None) -> ScreenResult:
    cfg = cfg or ScreenConfig()
    result = ScreenResult(as_of=as_of)

    # ---- Pass 1: universe -------------------------------------------------
    rows = provider.screener(
        filters=[
            ["market_capitalization", ">", cfg.min_market_cap],
            ["exchange", "=", "us"],
            ["avgvol_200d", ">", cfg.min_avg_volume],
        ],
        sort="market_capitalization.desc",
        limit=cfg.candidates,
    )
    universe = [row["code"] for row in rows]

    facts_by_ticker: dict[str, dict] = {}
    for ticker in universe:
        prices = provider.eod_prices(f"{ticker}.US", "2000-01-01", as_of)
        facts = extract_facts(
            ticker, provider.fundamentals(f"{ticker}.US"), prices, as_of
        )
        facts_by_ticker[ticker] = facts

        mom = facts["momentum_12_1"]
        if mom is None:
            result.rejections.append(
                Rejection(ticker, "momentum", "insufficient price history for 12-1")
            )
        elif mom < cfg.min_momentum_12_1:
            result.rejections.append(
                Rejection(ticker, "momentum", f"12-1 momentum {_pct(mom)} below floor")
            )

    passed_1 = [
        t
        for t, f in facts_by_ticker.items()
        if f["momentum_12_1"] is not None and f["momentum_12_1"] >= cfg.min_momentum_12_1
    ]

    # ---- Pass 2: growth durability ---------------------------------------
    passed_2 = []
    for ticker in passed_1:
        f = facts_by_ticker[ticker]
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

        if reasons:
            result.rejections.append(Rejection(ticker, "durability", "; ".join(reasons)))
        else:
            passed_2.append(ticker)

    # ---- Pass 3: exclusions ----------------------------------------------
    for ticker in passed_2:
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
        if dse is not None and dse < 0 and not cfg.allow_lookahead:
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
