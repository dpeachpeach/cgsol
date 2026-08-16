"""Who decides when an arriving issue becomes spend.

The cadence setting is the only control an operator has over ACU burn before a
scout runs, so the gate belongs on the event path and is worth pinning down.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings, TriageMode
from orchestrator.models import Issue
from orchestrator.service import Orchestrator
from orchestrator.state import Store
from orchestrator.webhooks import Debouncer


class FakeGitHub:
    async def get_issue(self, number: int) -> Issue:
        return Issue.model_validate(
            {"number": number, "title": "new bug", "labels": ["needs-triage"], "state": "open"}
        )


def orchestrator_for(mode: TriageMode) -> tuple[Orchestrator, list[set[int]]]:
    service = Orchestrator.__new__(Orchestrator)
    service.settings = Settings(replay=True, triage_mode=mode)
    service.store = Store()
    service.github = FakeGitHub()  # type: ignore[assignment]
    batches: list[set[int]] = []

    async def flush(batch: set[int]) -> None:
        batches.append(batch)

    service.debouncer = Debouncer(0.01, flush)
    return service, batches


def labeled_event() -> dict[str, Any]:
    return {
        "action": "labeled",
        "sender": {"login": "a-human"},
        "issue": {"number": 42, "labels": [{"name": "needs-triage"}]},
    }


async def test_auto_mode_queues_the_arriving_issue() -> None:
    service, _ = orchestrator_for(TriageMode.AUTO)
    assert await service.handle_event("issues", labeled_event()) == "queued"
    assert service.debouncer.pending == {42}


async def test_chunked_mode_records_the_issue_without_spending() -> None:
    """The sweep re-derives candidates from GitHub, so deferring loses nothing."""
    service, _ = orchestrator_for(TriageMode.CHUNKED)
    assert await service.handle_event("issues", labeled_event()) == "deferred"
    assert service.debouncer.pending == set()
    assert service.store.card(42) is not None


async def test_manual_mode_never_dispatches_on_a_webhook() -> None:
    service, _ = orchestrator_for(TriageMode.MANUAL)
    assert await service.handle_event("issues", labeled_event()) == "deferred"
    assert service.debouncer.pending == set()
