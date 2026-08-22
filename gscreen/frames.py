"""A universe built from SEC's XBRL *frames* API.

The per-company pipeline costs one price call per candidate, which caps a free
Tiingo key at a few hundred names a month. Frames inverts that: one request
returns a single fact for every filer in a period.

    data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2024.json

A dozen such calls give annual revenue across several years for roughly eight
thousand filers. Compute growth cross-sectionally, shortlist a couple of
hundred, and only then spend price calls on the survivors.

IMPORTANT - frames are NOT point-in-time.
Unlike companyfacts, a frame carries no `filed` date; the API picks the best
fact for each period regardless of when it became public, and later
restatements silently replace originals. That is fine for narrowing a
universe today, and it is a genuine look-ahead leak in a backtest: you would
be selecting candidates using figures that were not available at the
rebalance date. `FramesUniverse.point_in_time` is False for this reason and
the backtest warns rather than pretending otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import REVENUE_TAGS
from .providers import DEFAULT_UA, EDGAR_TICKERS, _Http

FRAMES = "https://data.sec.gov/api/xbrl/frames/us-gaap"


def is_common_stock(ticker: str) -> bool:
    """Filter share classes a growth screen has no business ranking.

    The first frames run surfaced ANG-PD and ATH-PA (preferred shares, whose
    "revenue growth" is the issuer's, not the security's) and VREOF / JETMF
    (five-letter OTC foreign ordinaries, thinly traded).
    """
    if "-" in ticker or "." in ticker:
        return False  # preferreds, warrants, units, dual-class suffixes
    if len(ticker) == 5 and ticker.endswith(("F", "Y")):
        return False  # OTC foreign ordinary / ADR
    return ticker.isalpha()


@dataclass
class FramesConfig:
    years: int = 3               # 3y CAGR needs years + 1 annual frames
    min_revenue: float = 50_000_000
    min_cagr: float = 0.20
    limit: int = 200             # how many candidates to hand downstream
    common_stock_only: bool = True
    tags: list[str] = field(default_factory=lambda: list(REVENUE_TAGS))


class FramesUniverse:
    """Cross-sectional revenue growth over every US filer that reports it."""

    point_in_time = False

    def __init__(
        self,
        as_of: str,
        cfg: FramesConfig | None = None,
        user_agent: str | None = None,
        cache_dir: str = "_cache/frames",
        scope: str | None = None,
    ) -> None:
        self.as_of = as_of
        self.cfg = cfg or FramesConfig()
        self.http = _Http(
            cache_dir=cache_dir,
            scope=scope or as_of,
            min_interval=0.15,
            headers={"User-Agent": user_agent or DEFAULT_UA, "Accept-Encoding": "gzip"},
        )
        self._tickers: dict[int, str] | None = None
        self.report: dict[str, int] = {}

    # -- helpers ----------------------------------------------------------
    def latest_complete_year(self) -> int:
        """Annual reports for year Y land in Q1 of Y+1. Before April, the most
        recently framed full year is still Y-2."""
        year, month = int(self.as_of[:4]), int(self.as_of[5:7])
        return year - 1 if month >= 4 else year - 2

    def ticker_for(self, cik: int) -> str | None:
        if self._tickers is None:
            payload = self.http.get_json(EDGAR_TICKERS)
            mapping: dict[int, str] = {}
            for row in payload.values():
                # first ticker wins; dual-class filers list several
                mapping.setdefault(int(row["cik_str"]), row["ticker"].upper())
            self._tickers = mapping
        return self._tickers.get(cik)

    def _frame(self, tag: str, period: str) -> dict[int, float]:
        """{cik: value} for one tag and period, or empty if the frame is absent."""
        try:
            payload = self.http.get_json(f"{FRAMES}/{tag}/USD/{period}.json")
        except Exception:  # noqa: BLE001 - a missing frame is normal
            return {}
        return {
            int(row["cik"]): float(row["val"])
            for row in payload.get("data", [])
            if row.get("cik") is not None and row.get("val") is not None
        }

    def revenue_by_year(self) -> dict[int, dict[int, float]]:
        """{cik: {year: revenue}} merged across the revenue tag variants.

        Filers use different tags; the first tag that reports a given
        company-year wins, which mirrors how `normalize.from_edgar` picks.
        """
        latest = self.latest_complete_year()
        years = range(latest - self.cfg.years, latest + 1)
        out: dict[int, dict[int, float]] = {}
        for year in years:
            for tag in self.cfg.tags:
                for cik, value in self._frame(tag, f"CY{year}").items():
                    out.setdefault(cik, {}).setdefault(year, value)
        return out

    # -- interface --------------------------------------------------------
    def universe(self, limit: int | None = None) -> list[str]:
        limit = limit or self.cfg.limit
        latest = self.latest_complete_year()
        first = latest - self.cfg.years
        by_cik = self.revenue_by_year()

        self.report = {"filers_with_revenue": len(by_cik)}
        scored: list[tuple[float, str]] = []
        incomplete = small = slow = unmapped = nonequity = 0

        for cik, series in by_cik.items():
            start, end = series.get(first), series.get(latest)
            if start is None or end is None:
                incomplete += 1
                continue
            if start <= 0 or end < self.cfg.min_revenue:
                small += 1
                continue
            cagr = (end / start) ** (1 / self.cfg.years) - 1
            if cagr < self.cfg.min_cagr:
                slow += 1
                continue
            ticker = self.ticker_for(cik)
            if not ticker:
                unmapped += 1  # private filers, funds, trusts
                continue
            if self.cfg.common_stock_only and not is_common_stock(ticker):
                nonequity += 1
                continue
            scored.append((cagr, ticker))

        self.report.update(
            {
                "rejected_incomplete_history": incomplete,
                "rejected_too_small": small,
                "rejected_slow_growth": slow,
                "rejected_no_ticker": unmapped,
                "rejected_not_common_stock": nonequity,
                "candidates": len(scored),
            }
        )

        scored.sort(reverse=True)
        return [ticker for _, ticker in scored[:limit]]

    def describe_funnel(self) -> str:
        if not self.report:
            return "universe() has not run yet"
        lines = [f"frames funnel (CY{self.latest_complete_year()} back {self.cfg.years}y):"]
        for key, value in self.report.items():
            lines.append(f"  {key:<28} {value:>7}")
        return "\n".join(lines)
