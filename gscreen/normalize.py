"""One normalised shape that every fundamentals source maps into.

Before this, the screen read EODHD's nested JSON directly, so adding a second
source meant either a second screen or a pile of branching. Each provider now
owns its own translation and the screen sees only `Fundamentals`.

`point_in_time` is the field that matters most. EDGAR stamps every fact with
the date it was filed, so a record built from EDGAR contains only what was
public on `as_of`. Vendor snapshot APIs cannot make that promise, and the
screen refuses to backtest on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fundamentals:
    ticker: str
    name: str | None = None
    sector: str | None = None
    market_cap: float | None = None
    ebitda: float | None = None
    profit_margin: float | None = None
    price_to_sales: float | None = None
    ev_to_sales: float | None = None
    short_pct_float: float | None = None
    last_earnings_date: str | None = None
    total_debt: float | None = None
    cash: float | None = None

    # oldest first
    annual_revenue: list[float] = field(default_factory=list)
    annual_fcf: list[float] = field(default_factory=list)
    annual_shares: list[float] = field(default_factory=list)
    # (period_end, revenue), oldest first
    quarterly_revenue: list[tuple[str, float]] = field(default_factory=list)

    point_in_time: bool = False
    source: str = "unknown"


def _num(value) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _walk(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _yearly(block: dict | None, key: str) -> list[float]:
    if not block:
        return []
    out = []
    for period in sorted(block):
        value = _num(block[period].get(key))
        if value is not None:
            out.append(value)
    return out


def from_eodhd(ticker: str, raw: dict) -> Fundamentals:
    """EODHD's nested fundamentals payload -> Fundamentals."""
    income_y = _walk(raw, "Financials", "Income_Statement", "yearly") or {}
    income_q = _walk(raw, "Financials", "Income_Statement", "quarterly") or {}
    balance_y = _walk(raw, "Financials", "Balance_Sheet", "yearly") or {}
    cash_y = _walk(raw, "Financials", "Cash_Flow", "yearly") or {}

    quarterly = []
    for period in sorted(income_q):
        value = _num(income_q[period].get("totalRevenue"))
        if value is not None:
            quarterly.append((period, value))

    debt = _yearly(balance_y, "shortLongTermDebtTotal")
    cash = _yearly(balance_y, "cashAndShortTermInvestments")

    return Fundamentals(
        ticker=ticker,
        name=_walk(raw, "General", "Name"),
        sector=_walk(raw, "General", "Sector"),
        market_cap=_num(_walk(raw, "Highlights", "MarketCapitalization")),
        ebitda=_num(_walk(raw, "Highlights", "EBITDA")),
        profit_margin=_num(_walk(raw, "Highlights", "ProfitMargin")),
        price_to_sales=_num(_walk(raw, "Valuation", "PriceSalesTTM")),
        ev_to_sales=_num(_walk(raw, "Valuation", "EnterpriseValueRevenue")),
        short_pct_float=_num(_walk(raw, "SharesStats", "ShortPercentFloat")),
        last_earnings_date=_walk(raw, "Earnings", "Last_Reported_Date"),
        total_debt=debt[-1] if debt else None,
        cash=cash[-1] if cash else None,
        annual_revenue=_yearly(income_y, "totalRevenue"),
        annual_fcf=_yearly(cash_y, "freeCashFlow"),
        annual_shares=_yearly(balance_y, "commonStockSharesOutstanding"),
        quarterly_revenue=quarterly,
        point_in_time=False,
        source="eodhd",
    )


# --------------------------------------------------------------------------
# EDGAR
# --------------------------------------------------------------------------

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
FCF_OPERATING = "NetCashProvidedByUsedInOperatingActivities"
FCF_CAPEX = "PaymentsToAcquirePropertyPlantAndEquipment"
SHARES_TAGS = ["CommonStockSharesOutstanding", "CommonStockSharesIssued"]
DEBT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebt", "DebtCurrent"]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
NET_INCOME = "NetIncomeLoss"


def _usd_facts(raw: dict, tag: str, taxonomy: str = "us-gaap") -> list[dict]:
    units = _walk(raw, "facts", taxonomy, tag, "units") or {}
    for unit_name in ("USD", "shares", "USD/shares"):
        if unit_name in units:
            return units[unit_name]
    return []


def _visible(facts: list[dict], as_of: str) -> list[dict]:
    """Only what had actually been filed by `as_of`. This is the whole point."""
    return [f for f in facts if f.get("filed") and f["filed"] <= as_of]


def _duration_facts(facts: list[dict], min_days: int, max_days: int) -> list[dict]:
    from datetime import date

    out = []
    for f in facts:
        if not f.get("start") or not f.get("end"):
            continue
        start = date.fromisoformat(f["start"])
        end = date.fromisoformat(f["end"])
        if min_days <= (end - start).days <= max_days:
            out.append(f)
    return out


def _latest_per_period(facts: list[dict]) -> dict[str, float]:
    """Keep the most recently filed value per period end - i.e. restatements
    win, but only restatements that existed by `as_of`."""
    best: dict[str, dict] = {}
    for f in facts:
        end = f["end"]
        if end not in best or f["filed"] > best[end]["filed"]:
            best[end] = f
    return {end: float(f["val"]) for end, f in best.items()}


def _first_available(raw: dict, tags: list[str], as_of: str) -> list[dict]:
    for tag in tags:
        facts = _visible(_usd_facts(raw, tag), as_of)
        if facts:
            return facts
    return []


def from_edgar(
    ticker: str,
    raw: dict,
    as_of: str,
    market_cap: float | None = None,
    short_pct_float: float | None = None,
) -> Fundamentals:
    """SEC XBRL companyfacts -> Fundamentals, filtered to what was public.

    market_cap and short_pct_float are not in EDGAR; pass them from a price
    source if you have them, otherwise valuation fields stay null and the
    model is told so.
    """
    revenue_facts = _first_available(raw, REVENUE_TAGS, as_of)
    annual_rev = _latest_per_period(_duration_facts(revenue_facts, 330, 400))
    quarterly_rev = _latest_per_period(_duration_facts(revenue_facts, 80, 100))

    ocf = _latest_per_period(
        _duration_facts(_visible(_usd_facts(raw, FCF_OPERATING), as_of), 330, 400)
    )
    capex = _latest_per_period(
        _duration_facts(_visible(_usd_facts(raw, FCF_CAPEX), as_of), 330, 400)
    )
    fcf = {
        period: ocf[period] - capex.get(period, 0.0)
        for period in sorted(ocf)
        if period in annual_rev
    }

    shares_facts = _first_available(raw, SHARES_TAGS, as_of)
    if not shares_facts:
        # Many filers report share count only in the dei taxonomy on the
        # cover page. Without this, dilution silently came back null.
        shares_facts = _visible(
            _usd_facts(raw, "EntityCommonStockSharesOutstanding", taxonomy="dei"),
            as_of,
        )
    shares = _latest_per_period([f for f in shares_facts if f.get("end")])

    debt_facts = _first_available(raw, DEBT_TAGS, as_of)
    debt = _latest_per_period([f for f in debt_facts if f.get("end")])
    cash_facts = _first_available(raw, CASH_TAGS, as_of)
    cash = _latest_per_period([f for f in cash_facts if f.get("end")])

    net_income = _latest_per_period(
        _duration_facts(_visible(_usd_facts(raw, NET_INCOME), as_of), 330, 400)
    )

    annual_periods = sorted(annual_rev)
    latest_revenue = annual_rev[annual_periods[-1]] if annual_periods else None
    latest_ni = net_income.get(annual_periods[-1]) if annual_periods else None

    filed_dates = [f["filed"] for f in revenue_facts if f.get("filed")]

    return Fundamentals(
        ticker=ticker,
        name=_walk(raw, "entityName"),
        sector=None,  # EDGAR carries SIC, not GICS sectors
        market_cap=market_cap,
        ebitda=None,  # not an XBRL concept; left null rather than guessed
        profit_margin=(
            latest_ni / latest_revenue
            if latest_ni is not None and latest_revenue
            else None
        ),
        price_to_sales=(
            market_cap / latest_revenue if market_cap and latest_revenue else None
        ),
        ev_to_sales=None,
        short_pct_float=short_pct_float,
        last_earnings_date=max(filed_dates) if filed_dates else None,
        total_debt=debt[max(debt)] if debt else None,
        cash=cash[max(cash)] if cash else None,
        annual_revenue=[annual_rev[p] for p in annual_periods],
        annual_fcf=[fcf[p] for p in sorted(fcf)],
        annual_shares=[shares[p] for p in sorted(shares)],
        quarterly_revenue=[(p, quarterly_rev[p]) for p in sorted(quarterly_rev)],
        point_in_time=True,
        source="edgar",
    )
