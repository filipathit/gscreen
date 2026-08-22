"""Growth-durability and momentum metrics.

The article filters on a single quarter of YoY revenue growth and a 5-day
price pop. Both are the noisiest available version of the thing they claim
to measure. Everything here is a replacement for one of those.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# Growth durability
# --------------------------------------------------------------------------


def revenue_cagr(annual_revenue: list[float], years: int = 3) -> float | None:
    """Compound annual revenue growth over `years`, oldest-first input.

    A single quarter can be inflated by one acquisition. A 3y CAGR cannot,
    at least not silently.
    """
    if len(annual_revenue) < years + 1:
        return None
    start, end = annual_revenue[-(years + 1)], annual_revenue[-1]
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def consecutive_growth_quarters(
    quarterly_yoy: list[float | None], threshold: float
) -> int:
    """How many of the most recent quarters cleared `threshold`, unbroken.

    Input is newest-first. One good print is a headline; six is a trend.
    """
    count = 0
    for value in quarterly_yoy:
        if value is None or value < threshold:
            break
        count += 1
    return count


def rule_of_40(revenue_growth: float | None, fcf_margin: float | None) -> float | None:
    """Growth + FCF margin. The standard test for growth that pays for itself."""
    if revenue_growth is None or fcf_margin is None:
        return None
    return revenue_growth + fcf_margin


def share_count_growth(shares: list[float]) -> float | None:
    """YoY dilution, oldest-first.

    Invisible in every metric the article uses. A company can grow revenue
    30% and still shrink your claim on it.
    """
    if len(shares) < 2 or shares[-2] <= 0:
        return None
    return shares[-1] / shares[-2] - 1


def net_debt_to_ebitda(
    total_debt: float | None, cash: float | None, ebitda: float | None
) -> float | None:
    """Leverage. The article narrates CoreWeave's debt but never screens on it."""
    if total_debt is None or cash is None or ebitda is None or ebitda == 0:
        return None
    return (total_debt - cash) / ebitda


# --------------------------------------------------------------------------
# Momentum
# --------------------------------------------------------------------------


def momentum_12_1(
    prices: list[dict], skip_days: int = 21, lookback_days: int = 252
) -> float | None:
    """Twelve-month return excluding the most recent month.

    Short windows (a week to a month) historically show reversal, not
    continuation. The skip-month is what separates a momentum screen from
    a chase-the-pop screen.
    """
    needed = skip_days + lookback_days + 1
    if len(prices) < needed:
        return None
    closes = [row["adjusted_close"] for row in prices]
    start = closes[-needed]
    end = closes[-(skip_days + 1)]
    if start <= 0:
        return None
    return end / start - 1


def realised_volatility(prices: list[dict], window: int = 60) -> float | None:
    """Annualised daily vol over the trailing window - a crude quality gate."""
    closes = [row["adjusted_close"] for row in prices][-(window + 1) :]
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var**0.5) * (252**0.5)


def forward_return(prices: list[dict], start: str, horizon_days: int) -> float | None:
    """Return from `start` over the next `horizon_days` sessions. Backtest only."""
    idx = next((i for i, r in enumerate(prices) if r["date"] >= start), None)
    if idx is None or idx + horizon_days >= len(prices):
        return None
    a = prices[idx]["adjusted_close"]
    b = prices[idx + horizon_days]["adjusted_close"]
    return b / a - 1 if a > 0 else None


# --------------------------------------------------------------------------
# Exclusions the article claims but never implements
# --------------------------------------------------------------------------


def days_since_earnings(as_of: str, last_earnings: str | None) -> int | None:
    """A 5-day pop days after a print is an earnings reaction, not a trend."""
    if not last_earnings:
        return None
    a = datetime.strptime(as_of, "%Y-%m-%d").date()
    b = datetime.strptime(last_earnings, "%Y-%m-%d").date()
    return (a - b).days


def squeeze_risk(short_pct_float: float | None, threshold: float = 0.15) -> bool | None:
    """The article says pass 2 rules out short squeezes. Nothing in its code
    looks at short interest. This does."""
    if short_pct_float is None:
        return None
    return short_pct_float >= threshold


def trading_days_between(start: str, end: str) -> int:
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    days = 0
    cur: date = d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days
