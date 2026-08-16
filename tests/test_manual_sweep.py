"""Pressing refresh has to ask GitHub, not the projection.

The button re-read `/api/state`, which is the orchestrator's own store — so a
label changed in the GitHub UI stayed invisible for up to a reconcile interval
however many times it was pressed, and the board looked broken rather than
merely behind. A person presses refresh precisely when they suspect the
projection is stale, so that is the one moment it must not be trusted.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from orchestrator import main
from orchestrator.config import Settings
from orchestrator.labels import State
from orchestrator.models import Issue
from orchestrator.poller import Poller
from orchestrator.service import Orchestrator
from orchestrator.state import Store


class RecordingPoller:
    """Stands in for the sweep, and records that it was asked to run."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.sweeps = 0

    async def reconcile(self) -> None:
        self.sweeps += 1
        self.store.upsert_issue(
            Issue.model_validate(
                {
                    "number": 7,
                    "title": "seen only by a sweep",
                    "labels": [State.DEVIN_ELIGIBLE.value],
                    "state": "open",
                }
            )
        )


def service_with(poller: RecordingPoller) -> Orchestrator:
    service = Orchestrator.__new__(Orchestrator)
    service.settings = Settings(replay=True)
    service.store = poller.store
    service.poller = poller  # type: ignore[assignment]
    return service


def test_refresh_sweeps_github_and_returns_what_it_found() -> None:
    poller = RecordingPoller(Store())
    main.orchestrator = service_with(poller)
    try:
        # No lifespan: the app under test is the routing, not a live pipeline.
        client = TestClient(main.app)
        assert client.get("/api/state").json()["cards"] == []
        body = client.post("/api/reconcile").json()
    finally:
        main.orchestrator = None

    assert poller.sweeps == 1
    assert [card["number"] for card in body["cards"]] == [7]


def test_reading_the_state_never_sweeps() -> None:
    """`/api/state` is on the SSE path, which fires per event — sweeping there
    would turn one webhook into a GitHub read and undo the budget work."""
    poller = RecordingPoller(Store())
    main.orchestrator = service_with(poller)
    try:
        client = TestClient(main.app)
        client.get("/api/state")
        client.get("/api/state")
    finally:
        main.orchestrator = None

    assert poller.sweeps == 0


async def test_two_refreshes_at_once_only_read_github_once() -> None:
    """The lock is not politeness: concurrent sweeps race on the `since`
    watermark, and the loser's window is silently skipped."""
    poller = Poller.__new__(Poller)
    poller._sweeping = asyncio.Lock()
    running = 0
    peak = 0

    async def sweep(full: bool | None) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1

    poller._reconcile = sweep  # type: ignore[method-assign]

    await asyncio.gather(poller.reconcile(), poller.reconcile(), poller.reconcile())
    assert peak == 1
