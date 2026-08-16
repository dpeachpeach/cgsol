"""The hourly REST budget, which a polling orchestrator can spend in minutes.

Found the hard way: reconciling a 30-issue fork every minute exhausted 5000
requests in under an hour, and every read after that 403'd — the board emptied
itself because a projection built from failed reads is worse than a stale one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from orchestrator import github as github_module
from orchestrator.config import Settings
from orchestrator.github import WRITE_RESERVE, GitHubClient, RateLimited

REPO = "dpeachpeach/superset-cg"


def _record(slept: list[float]) -> Callable[[float], Coroutine[Any, Any, None]]:
    """Stand in for `asyncio.sleep`, so a test of pacing does not have to wait
    out the pacing."""

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep


def client_with(handler: httpx.MockTransport) -> GitHubClient:
    settings = Settings(replay=True, github_repo=REPO, github_token="ghp_x")
    client = GitHubClient(settings, tokens=None)
    client._client = httpx.AsyncClient(base_url=settings.github_api_url, transport=handler)
    return client


def limits(remaining: int, resets_in: int = 900) -> dict[str, str]:
    return {
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(time.time()) + resets_in),
    }


async def test_an_unchanged_resource_is_re_read_for_free() -> None:
    """A 304 is not charged to the budget, so conditional reads are the only
    way a per-minute sweep is affordable."""
    served = []

    def handle(request: httpx.Request) -> httpx.Response:
        served.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == 'W/"abc"':
            return httpx.Response(304, headers=limits(4000))
        return httpx.Response(
            200,
            json=[{"number": 7, "title": "t", "labels": [], "state": "open"}],
            headers={"etag": 'W/"abc"', **limits(3999)},
        )

    client = client_with(httpx.MockTransport(handle))
    first = await client.list_issues()
    second = await client.list_issues()

    assert [i.number for i in first] == [i.number for i in second] == [7]
    assert served == [None, 'W/"abc"']  # second read was conditional


async def test_reads_stand_down_before_writes_do() -> None:
    """With the budget nearly gone, a label write still has to land: a stale
    board recovers on the next sweep, a dropped transition never does."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=[], headers=limits(WRITE_RESERVE - 1))

    client = client_with(httpx.MockTransport(handle))
    await client.list_issues()  # learns the remaining budget from the response

    with pytest.raises(RateLimited):
        await client.list_issues()
    await client.add_labels(7, ["needs-triage"])
    assert calls == ["GET", "POST"]


async def test_a_cold_process_learns_the_budget_is_gone_from_the_403() -> None:
    """A restart has no counter yet, so the first request goes out and comes
    back 403. It has to surface as a rate limit, not a generic HTTP error, or
    startup treats an hour of thin budget as an hour of downtime."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "API rate limit exceeded"}, headers=limits(0, resets_in=300)
        )

    client = client_with(httpx.MockTransport(handle))
    with pytest.raises(RateLimited):
        await client.list_issues()


async def test_pacing_costs_nothing_while_the_budget_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full budget spread over a full hour is under a second a read. Taking
    that gap would only slow every sweep down without changing whether the hour
    survives, so it is not taken."""
    slept: list[float] = []
    monkeypatch.setattr(github_module.asyncio, "sleep", _record(slept))

    client = client_with(httpx.MockTransport(lambda request: httpx.Response(204)))
    client._remaining, client._resets_at = 5000, time.time() + 3600
    for _ in range(20):
        await client._pace()

    assert slept == []


async def test_pacing_throttles_a_thin_budget_to_fit_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 200 requests and most of an hour to cover, reads have to be spaced
    out — the alternative is spending them in a minute and going blind."""
    slept: list[float] = []
    monkeypatch.setattr(github_module.asyncio, "sleep", _record(slept))

    client = client_with(httpx.MockTransport(lambda request: httpx.Response(204)))
    client._remaining, client._resets_at = 200, time.time() + 3600
    await client._pace()
    await client._pace()

    # 3600s over the 100 requests not held back for writes.
    assert slept and slept[-1] == pytest.approx(36, abs=1)


async def test_an_exhausted_budget_stops_everything_and_says_when_it_returns() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers=limits(0, resets_in=600))

    client = client_with(httpx.MockTransport(handle))
    await client.list_issues()

    assert client.rate_limited_until > time.time()
    with pytest.raises(RateLimited) as caught:
        await client.add_labels(7, ["needs-triage"])
    assert caught.value.resets_at == pytest.approx(client.rate_limited_until)


async def test_the_raw_and_json_views_of_a_file_are_cached_apart() -> None:
    """Same URL, two representations. Keying the conditional cache on the URL
    alone replayed the raw YAML as the answer to the metadata read, and the
    commit path died parsing it — settings could be read but never written."""

    def handle(request: httpx.Request) -> httpx.Response:
        raw = "raw" in request.headers.get("accept", "")
        if request.headers.get("if-none-match") == ('W/"raw"' if raw else 'W/"json"'):
            return httpx.Response(304, headers=limits(4000))
        if raw:
            return httpx.Response(
                200, text="max_concurrent_workers: 0\n", headers={"etag": 'W/"raw"', **limits(3999)}
            )
        return httpx.Response(
            200, json={"sha": "deadbeef"}, headers={"etag": 'W/"json"', **limits(3998)}
        )

    client = client_with(httpx.MockTransport(handle))
    assert await client.get_file(".cgsol/config.yaml") == "max_concurrent_workers: 0\n"
    head = await client._request("GET", f"/repos/{REPO}/contents/.cgsol/config.yaml")
    assert head.json()["sha"] == "deadbeef"
    # And again, now that both are cached and both answer 304.
    assert await client.get_file(".cgsol/config.yaml") == "max_concurrent_workers: 0\n"
    head = await client._request("GET", f"/repos/{REPO}/contents/.cgsol/config.yaml")
    assert head.json()["sha"] == "deadbeef"
