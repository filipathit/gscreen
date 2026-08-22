"""Data providers, split by capability.

Three capabilities, independently swappable at runtime:

  universe      - which tickers to consider   (eodhd | static | fixture)
  prices        - daily adjusted closes       (eodhd | yahoo | fixture)
  fundamentals  - financial statements        (eodhd | edgar | fixture)

`CompositeProvider` glues one of each together, so "free prices from Yahoo,
point-in-time fundamentals from EDGAR, universe from a file" is a valid
configuration and costs nothing.

Honest notes on the free sources:
  Yahoo has had no official API since 2017. These are the endpoints the site
  itself calls; they rate-limit hard, and datacenter IPs (CI runners) get hit
  worst. Treat 429s as expected, not exceptional.
  EDGAR is official and free but wants a descriptive User-Agent with a
  contact address, and covers US filers only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

import requests

from .normalize import Fundamentals, from_edgar, from_eodhd

EODHD_BASE = "https://eodhd.com/api"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote"
EDGAR_FACTS = "https://data.sec.gov/api/xbrl/companyfacts"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"

DEFAULT_UA = os.environ.get(
    "SEC_USER_AGENT", "gscreen research script (set SEC_USER_AGENT to your email)"
)


def serialise_filters(filters: list[list[Any]]) -> str:
    """JSON, not str(). str() on a list emits single quotes and the screener
    endpoint rejects it - the bug the source article ships."""
    return json.dumps(filters, separators=(",", ":"))


# --------------------------------------------------------------------------
# Capability protocols
# --------------------------------------------------------------------------


class UniverseSource(Protocol):
    def universe(self, limit: int) -> list[str]: ...


class PriceSource(Protocol):
    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]: ...


class FundamentalsSource(Protocol):
    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals: ...


# --------------------------------------------------------------------------
# Shared HTTP plumbing
# --------------------------------------------------------------------------


class _Http:
    def __init__(
        self,
        cache_dir: str | Path | None = ".cache",
        min_interval: float = 0.15,
        max_retries: int = 4,
        headers: dict | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last = 0.0
        self._session = requests.Session()
        if headers:
            self._session.headers.update(headers)

    def get_json(self, url: str, params: dict | None = None, secret_keys=()) -> Any:
        params = params or {}
        cache = None
        if self.cache_dir:
            safe = {k: v for k, v in params.items() if k not in secret_keys}
            key = hashlib.sha256(f"{url}{sorted(safe.items())}".encode()).hexdigest()[:20]
            cache = self.cache_dir / f"{key}.json"
            if cache.exists():
                return json.loads(cache.read_text())

        last: Exception | None = None
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._session.get(url, params=params, timeout=30)
                if resp.status_code in (429, 503):
                    time.sleep(2**attempt + 1)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if cache:
                    cache.write_text(json.dumps(payload))
                return payload
            except requests.RequestException as exc:
                last = exc
                time.sleep(2**attempt)
        raise RuntimeError(
            f"request failed after {self.max_retries} tries: {url}"
        ) from last


# --------------------------------------------------------------------------
# EODHD (paid)
# --------------------------------------------------------------------------


class EODHDProvider:
    point_in_time = False

    def __init__(self, api_key: str | None = None, cache_dir="_cache/eodhd") -> None:
        self.api_key = api_key or os.environ.get("EODHD_API_KEY")
        if not self.api_key:
            raise RuntimeError("EODHD_API_KEY is not set.")
        self.http = _Http(cache_dir=cache_dir)

    def _get(self, path: str, params: dict) -> Any:
        return self.http.get_json(
            f"{EODHD_BASE}/{path}",
            {**params, "api_token": self.api_key, "fmt": "json"},
            secret_keys=("api_token",),
        )

    def universe(self, limit: int) -> list[str]:
        payload = self._get(
            "screener",
            {
                "filters": serialise_filters(
                    [
                        ["market_capitalization", ">", 2_000_000_000],
                        ["exchange", "=", "us"],
                        ["avgvol_200d", ">", 1_000_000],
                    ]
                ),
                "sort": "market_capitalization.desc",
                "limit": limit,
            },
        )
        return [row["code"] for row in payload.get("data", [])]

    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        rows = self._get(f"eod/{ticker}.US", {"from": start, "to": end, "period": "d"})
        return [
            {"date": r["date"], "adjusted_close": float(r["adjusted_close"])}
            for r in rows
        ]

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals:
        return from_eodhd(ticker, self._get(f"fundamentals/{ticker}.US", {}))


# --------------------------------------------------------------------------
# Yahoo (free prices, unofficial)
# --------------------------------------------------------------------------


class YahooPrices:
    """Daily adjusted closes from the endpoint the Yahoo website itself calls.

    Free and deep - decades of history, more than EODHD's free tier gives.
    Unofficial, so 429s are expected rather than exceptional, and worst from
    shared cloud IPs.
    """

    def __init__(self, cache_dir="_cache/yahoo") -> None:
        self.http = _Http(
            cache_dir=cache_dir,
            min_interval=1.2,  # deliberately slow; politeness is the only fix
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
                )
            },
        )

    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        from datetime import datetime, timezone

        def epoch(day: str) -> int:
            return int(
                datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
            )

        payload = self.http.get_json(
            f"{YAHOO_CHART}/{ticker}",
            {
                "period1": epoch(start),
                "period2": epoch(end),
                "interval": "1d",
                "events": "div,split",
            },
        )
        return parse_yahoo_chart(payload)

    def market_cap(self, ticker: str) -> float | None:
        try:
            payload = self.http.get_json(YAHOO_QUOTE, {"symbols": ticker})
            rows = (payload.get("quoteResponse") or {}).get("result") or []
            return rows[0].get("marketCap") if rows else None
        except RuntimeError:
            return None


def parse_yahoo_chart(payload: dict) -> list[dict]:
    """Pulled out of the class so it can be tested without a network call."""
    from datetime import datetime, timezone

    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    block = result[0]
    stamps = block.get("timestamp") or []
    adj_blocks = (block.get("indicators") or {}).get("adjclose") or [{}]
    closes = adj_blocks[0].get("adjclose") or []

    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        out.append({"date": day, "adjusted_close": float(close)})
    return out


# --------------------------------------------------------------------------
# EDGAR (free, official, point-in-time)
# --------------------------------------------------------------------------


class EdgarFundamentals:
    """SEC XBRL company facts.

    The only free source here that stamps every number with its filing date,
    which is what makes an honest backtest possible. US filers only.
    """

    point_in_time = True

    def __init__(self, user_agent: str | None = None, cache_dir="_cache/edgar") -> None:
        self.http = _Http(
            cache_dir=cache_dir,
            min_interval=0.15,  # SEC asks for no more than ~10 requests/second
            headers={"User-Agent": user_agent or DEFAULT_UA, "Accept-Encoding": "gzip"},
        )
        self._cik: dict[str, str] | None = None

    def cik_for(self, ticker: str) -> str | None:
        if self._cik is None:
            payload = self.http.get_json(EDGAR_TICKERS)
            self._cik = {
                row["ticker"].upper(): f"{int(row['cik_str']):010d}"
                for row in payload.values()
            }
        return self._cik.get(ticker.upper())

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals:
        cik = self.cik_for(ticker)
        if not cik:
            return Fundamentals(ticker=ticker, source="edgar", point_in_time=True)
        raw = self.http.get_json(f"{EDGAR_FACTS}/CIK{cik}.json")
        return from_edgar(ticker, raw, as_of)


# --------------------------------------------------------------------------
# Static universe
# --------------------------------------------------------------------------


class StaticUniverse:
    """Tickers from a file, one per line. Blank lines and # comments ignored.

    Not a substitute for a screener - it discovers nothing. It is what lets
    the whole pipeline run for free, and it makes the universe explicit and
    reviewable, which a screener call never is.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def universe(self, limit: int) -> list[str]:
        lines = self.path.read_text().splitlines()
        tickers = [
            line.strip().upper()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        return tickers[:limit]


# --------------------------------------------------------------------------
# Fixtures (offline)
# --------------------------------------------------------------------------


class FixtureProvider:
    point_in_time = False

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._screener = json.loads((self.root / "screener.json").read_text())
        self._fundamentals = json.loads((self.root / "fundamentals.json").read_text())
        self._prices = json.loads((self.root / "prices.json").read_text())

    def universe(self, limit: int) -> list[str]:
        rows = sorted(
            self._screener, key=lambda r: r["market_capitalization"], reverse=True
        )
        return [r["code"] for r in rows[:limit]]

    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        series = self._prices[ticker.replace(".US", "")]
        return [row for row in series if start <= row["date"] <= end]

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals:
        return from_eodhd(ticker, self._fundamentals[ticker.replace(".US", "")])


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


class CompositeProvider:
    def __init__(
        self,
        universe_source: UniverseSource,
        price_source: PriceSource,
        fundamentals_source: FundamentalsSource,
    ) -> None:
        self.universe_source = universe_source
        self.price_source = price_source
        self.fundamentals_source = fundamentals_source

    @property
    def point_in_time(self) -> bool:
        return bool(getattr(self.fundamentals_source, "point_in_time", False))

    def describe(self) -> str:
        def name(obj):
            return type(obj).__name__

        return (
            f"universe={name(self.universe_source)}  "
            f"prices={name(self.price_source)}  "
            f"fundamentals={name(self.fundamentals_source)}  "
            f"point_in_time={self.point_in_time}"
        )

    def universe(self, limit: int) -> list[str]:
        return self.universe_source.universe(limit)

    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        return self.price_source.eod_prices(ticker, start, end)

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals:
        return self.fundamentals_source.fundamentals(ticker, as_of)


SOURCES = {
    "universe": ("eodhd", "static", "fixture"),
    "prices": ("eodhd", "yahoo", "fixture"),
    "fundamentals": ("eodhd", "edgar", "fixture"),
}

PRESETS = {
    # name: (universe, prices, fundamentals)
    "offline": ("fixture", "fixture", "fixture"),
    "free": ("static", "yahoo", "edgar"),
    "paid": ("eodhd", "eodhd", "eodhd"),
    "hybrid": ("eodhd", "eodhd", "edgar"),  # paid discovery, honest fundamentals
}


def build_provider(
    universe: str,
    prices: str,
    fundamentals: str,
    fixtures_dir: str | Path,
    universe_file: str | Path,
) -> CompositeProvider:
    """Assemble a provider from three independent runtime choices."""
    cached: dict[str, Any] = {}

    def get_fixture():
        cached.setdefault("fixture", FixtureProvider(fixtures_dir))
        return cached["fixture"]

    def get_eodhd():
        cached.setdefault("eodhd", EODHDProvider())
        return cached["eodhd"]

    universe_source = {
        "eodhd": get_eodhd,
        "static": lambda: StaticUniverse(universe_file),
        "fixture": get_fixture,
    }[universe]()

    price_source = {
        "eodhd": get_eodhd,
        "yahoo": YahooPrices,
        "fixture": get_fixture,
    }[prices]()

    fundamentals_source = {
        "eodhd": get_eodhd,
        "edgar": EdgarFundamentals,
        "fixture": get_fixture,
    }[fundamentals]()

    return CompositeProvider(universe_source, price_source, fundamentals_source)
