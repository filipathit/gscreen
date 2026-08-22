from __future__ import annotations

import json
from pathlib import Path

import pytest

from gscreen.providers import _Http, prune_cache


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class CountingHttp(_Http):
    """_Http with the network replaced by a counter."""

    def __init__(self, *args, payload=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self._payload = payload or {"value": 1}

        class Session:
            def __init__(self, outer):
                self.outer = outer
                self.headers = {}

            def get(self, url, params=None, timeout=None):
                self.outer.calls += 1
                return FakeResponse(self.outer._payload)

        self._session = Session(self)


def test_second_call_same_day_is_served_from_cache(tmp_path):
    http = CountingHttp(cache_dir=tmp_path, scope="2026-08-21")
    assert http.get_json("https://example.com/x") == {"value": 1}
    assert http.get_json("https://example.com/x") == {"value": 1}
    assert http.calls == 1, "second same-day call should not hit the network"


def test_a_new_day_refetches(tmp_path):
    """EDGAR companyfacts URLs carry no date. Cached forever, they would serve
    pre-filing figures indefinitely."""
    day_one = CountingHttp(cache_dir=tmp_path, scope="2026-08-21")
    day_one.get_json("https://example.com/x")
    assert day_one.calls == 1

    day_two = CountingHttp(cache_dir=tmp_path, scope="2026-08-24")
    day_two.get_json("https://example.com/x")
    assert day_two.calls == 1, "a new day must not reuse yesterday's response"


def test_different_params_are_cached_separately(tmp_path):
    http = CountingHttp(cache_dir=tmp_path, scope="2026-08-21")
    http.get_json("https://example.com/x", {"ticker": "AAA"})
    http.get_json("https://example.com/x", {"ticker": "BBB"})
    http.get_json("https://example.com/x", {"ticker": "AAA"})
    assert http.calls == 2


def test_secrets_never_reach_the_cache_key(tmp_path):
    http = CountingHttp(cache_dir=tmp_path, scope="2026-08-21")
    http.get_json("https://example.com/x", {"api_token": "SECRET", "s": "AAA"},
                  secret_keys=("api_token",))
    # a different token must hit the same cache entry
    http.get_json("https://example.com/x", {"api_token": "OTHER", "s": "AAA"},
                  secret_keys=("api_token",))
    assert http.calls == 1
    written = list(Path(tmp_path).rglob("*.json"))
    assert written
    assert all("SECRET" not in f.name for f in written)


def test_text_cache_round_trips(tmp_path):
    class TextHttp(CountingHttp):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)

            class Session:
                def __init__(self, outer):
                    self.outer = outer
                    self.headers = {}

                def get(self, url, params=None, timeout=None):
                    self.outer.calls += 1
                    resp = FakeResponse({})
                    resp.text = "Date,Close\n2025-01-02,10\n"
                    return resp

            self._session = Session(self)

    http = TextHttp(cache_dir=tmp_path, scope="2026-08-21")
    first = http.get_text("https://example.com/csv")
    second = http.get_text("https://example.com/csv")
    assert first == second
    assert http.calls == 1


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------


def test_prune_keeps_the_newest_days_only(tmp_path):
    for day in ["2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]:
        d = tmp_path / day
        d.mkdir()
        (d / "a.json").write_text("{}")

    removed = prune_cache(tmp_path, keep_days=2)
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["2026-08-20", "2026-08-21"]
    assert sorted(removed) == ["2026-08-18", "2026-08-19"]


def test_prune_is_safe_on_a_missing_directory(tmp_path):
    assert prune_cache(tmp_path / "nope") == []


def test_constructing_http_prunes_old_days(tmp_path):
    for day in ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]:
        (tmp_path / day).mkdir()
    CountingHttp(cache_dir=tmp_path, scope="2026-08-21", keep_days=2)
    assert len(list(tmp_path.iterdir())) <= 3  # 2 kept + today's
