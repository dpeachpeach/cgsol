"""The hourly REST budget, which a polling orchestrator can spend in minutes.

Found the hard way: reconciling a 30-issue fork every minute exhausted 5000
requests in under an hour, and every read after that 403'd — the board emptied
itself because a projection built from failed reads is worse than a stale one.
"""

from __future__ import annotations

import time

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.github import WRITE_RESERVE, GitHubClient, RateLimited

REPO = "dpeachpeach/superset-cg"


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


async def test_an_exhausted_budget_stops_everything_and_says_when_it_returns() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers=limits(0, resets_in=600))

    client = client_with(httpx.MockTransport(handle))
    await client.list_issues()

    assert client.rate_limited_until > time.time()
    with pytest.raises(RateLimited) as caught:
        await client.add_labels(7, ["needs-triage"])
    assert caught.value.resets_at == pytest.approx(client.rate_limited_until)
