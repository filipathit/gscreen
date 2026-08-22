"""Data providers.

Two implementations behind one interface:

* ``EODHDProvider``  - live calls (needs EODHD_API_KEY in the environment)
* ``FixtureProvider`` - reads local JSON, so the pipeline and its tests run
  offline and deterministically.

Fixes applied vs. the article's version:
  - filters are serialised with json.dumps (str(list) emits single quotes,
    which is not valid JSON and the screener endpoint rejects it)
  - API key comes from the environment, never a literal in source
  - retry with backoff, explicit timeouts, raise_for_status everywhere
  - on-disk response cache so a backtest does not re-bill every rerun
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

import requests

BASE_URL = "https://eodhd.com/api"


def serialise_filters(filters: list[list[Any]]) -> str:
    """Serialise screener filters as JSON.

    THE BUG THE ARTICLE SHIPS:
        str([["exchange", "=", "us"]])  ->  [['exchange', '=', 'us']]
    Single quotes. Not JSON. Use json.dumps.
    """
    return json.dumps(filters, separators=(",", ":"))


class Provider(Protocol):
    def screener(self, filters: list[list[Any]], sort: str, limit: int) -> list[dict]: ...
    def fundamentals(self, ticker: str) -> dict: ...
    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]: ...


class EODHDProvider:
    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = ".cache",
        min_interval: float = 0.12,
        max_retries: int = 4,
    ) -> None:
        self.api_key = api_key or os.environ.get("EODHD_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "Set EODHD_API_KEY in your environment (do not hardcode it)."
            )
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_call = 0.0
        self._session = requests.Session()

    # -- plumbing ---------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _cache_path(self, url: str, params: dict) -> Path | None:
        if not self.cache_dir:
            return None
        safe = {k: v for k, v in params.items() if k != "api_token"}
        key = hashlib.sha256(f"{url}{sorted(safe.items())}".encode()).hexdigest()[:20]
        return self.cache_dir / f"{key}.json"

    def _get(self, path: str, params: dict) -> Any:
        url = f"{BASE_URL}/{path}"
        cache = self._cache_path(url, params)
        if cache and cache.exists():
            return json.loads(cache.read_text())

        params = {**params, "api_token": self.api_key, "fmt": "json"}
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if cache:
                    cache.write_text(json.dumps(payload))
                return payload
            except requests.RequestException as exc:  # noqa: PERF203
                last_error = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"EODHD request failed after retries: {path}") from last_error

    # -- interface --------------------------------------------------------
    def screener(self, filters: list[list[Any]], sort: str, limit: int) -> list[dict]:
        payload = self._get(
            "screener",
            {"filters": serialise_filters(filters), "sort": sort, "limit": limit},
        )
        return payload.get("data", [])

    def fundamentals(self, ticker: str) -> dict:
        return self._get(f"fundamentals/{ticker}", {})

    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        return self._get(f"eod/{ticker}", {"from": start, "to": end, "period": "d"})


class FixtureProvider:
    """Offline provider. Same interface, JSON on disk."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._screener = json.loads((self.root / "screener.json").read_text())
        self._fundamentals = json.loads((self.root / "fundamentals.json").read_text())
        self._prices = json.loads((self.root / "prices.json").read_text())

    def screener(self, filters: list[list[Any]], sort: str, limit: int) -> list[dict]:
        rows = self._screener
        for field, op, value in filters:
            rows = [r for r in rows if _passes(r.get(field), op, value)]
        field, _, direction = sort.partition(".")
        rows = sorted(
            rows, key=lambda r: r.get(field) or 0, reverse=(direction == "desc")
        )
        return rows[:limit]

    def fundamentals(self, ticker: str) -> dict:
        return self._fundamentals[ticker.replace(".US", "")]

    def eod_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        series = self._prices[ticker.replace(".US", "")]
        return [row for row in series if start <= row["date"] <= end]


def _passes(actual: Any, op: str, expected: Any) -> bool:
    if actual is None:
        return False
    return {
        ">": lambda: actual > expected,
        ">=": lambda: actual >= expected,
        "<": lambda: actual < expected,
        "<=": lambda: actual <= expected,
        "=": lambda: actual == expected,
    }[op]()
