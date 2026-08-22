"""Trading-day handling for `as_of`.

`as_of` is not a label. It is the right edge of the momentum window and the
cutoff for which SEC filings count as public, so pointing it at a Saturday
shifts both by up to two days without saying so.

No exchange calendar here on purpose: weekends are handled exactly, holidays
are not, and pretending otherwise would be worse than being explicit about it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# US market holidays are not modelled. If a run lands on one, the price
# source simply has no row for it and the effect is the same as a weekend.
WEEKEND = (5, 6)


def is_trading_day(day: str) -> bool:
    return datetime.strptime(day, "%Y-%m-%d").date().weekday() not in WEEKEND


def previous_trading_day(day: str) -> str:
    """Step back to the most recent weekday, inclusive."""
    current = datetime.strptime(day, "%Y-%m-%d").date()
    while current.weekday() in WEEKEND:
        current -= timedelta(days=1)
    return current.isoformat()


def resolve_as_of(day: str | None = None, today: date | None = None) -> tuple[str, str | None]:
    """Return (as_of, note). The note is non-null when the date was moved."""
    raw = day or (today or date.today()).isoformat()
    snapped = previous_trading_day(raw)
    if snapped == raw:
        return raw, None
    return snapped, f"{raw} is a weekend; using the previous trading day {snapped}"
