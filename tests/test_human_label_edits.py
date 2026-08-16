"""A maintainer editing labels in the GitHub UI is giving an instruction.

The state label was always read back. The escalation was not: it lived in the
metadata comment, so removing `escalation:ci-unfixable` by hand cleared the tag
on the board while the dispatcher went on refusing to retry the issue. A label
a human can remove and an agent then ignores is worse than no label at all.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings, TriageMode
from orchestrator.labels import State
from orchestrator.models import Issue, IssueMeta
from orchestrator.service import Orchestrator
from orchestrator.state import Store
from orchestrator.webhooks import Debouncer


class FakeGitHub:
    def __init__(self, labels: list[str]) -> None:
        self.labels = labels
        self.meta_writes: list[tuple[int, IssueMeta, str]] = []

    async def get_issue(self, number: int) -> Issue:
        return Issue.model_validate(
            {"number": number, "title": "sort-only metric", "labels": self.labels, "state": "open"}
        )

    async def upsert_meta(self, number: int, meta: IssueMeta, note: str) -> None:
        self.meta_writes.append((number, meta, note))


def service_with(labels: list[str], escalation: str | None) -> tuple[Orchestrator, FakeGitHub]:
    service = Orchestrator.__new__(Orchestrator)
    service.settings = Settings(replay=True, triage_mode=TriageMode.MANUAL)
    service.store = Store()
    github = FakeGitHub(labels)
    service.github = github  # type: ignore[assignment]
    service.debouncer = Debouncer(0.01, lambda batch: _noop())
    card = service.store.upsert_issue(
        Issue.model_validate(
            {"number": 25, "title": "sort-only metric", "labels": labels, "state": "open"}
        )
    )
    card.meta.escalation = escalation
    card.meta.session_id = "devin-abc"
    return service, github


async def _noop() -> None:
    return None


def unlabeled_event(labels: list[str], sender: str = "a-human") -> dict[str, Any]:
    return {
        "action": "unlabeled",
        "sender": {"login": sender},
        "issue": {"number": 25, "labels": [{"name": name} for name in labels]},
    }


async def test_removing_the_escalation_label_puts_the_issue_back_in_the_pipeline() -> None:
    service, github = service_with(["human-review", "tier:medium"], "ci-unfixable")

    assert await service.handle_event("issues", unlabeled_event(["human-review"])) == "refreshed"

    card = service.store.card(25)
    assert card is not None
    assert card.meta.escalation is None
    number, meta, note = github.meta_writes[-1]
    assert (number, meta.escalation) == (25, None)
    assert "ci-unfixable" in note


async def test_adding_one_by_hand_is_recorded_the_same_way() -> None:
    service, github = service_with(
        ["human-review", "escalation:needs-approval"],
        None,
    )
    event = unlabeled_event(["human-review", "escalation:needs-approval"])
    event["action"] = "labeled"

    await service.handle_event("issues", event)

    card = service.store.card(25)
    assert card is not None and card.meta.escalation == "needs-approval"
    assert github.meta_writes[-1][1].escalation == "needs-approval"


async def test_an_unchanged_escalation_costs_no_write() -> None:
    """Every label the orchestrator itself sets comes back as an event too."""
    service, github = service_with(
        ["human-review", "escalation:ci-unfixable"],
        "ci-unfixable",
    )

    await service.handle_event(
        "issues", unlabeled_event(["human-review", "escalation:ci-unfixable"])
    )

    assert github.meta_writes == []


async def test_a_state_label_edited_by_hand_is_the_new_state() -> None:
    service, _ = service_with(["human-review"], None)
    service.github.labels = ["devin-eligible"]  # type: ignore[attr-defined]

    await service.handle_event("issues", unlabeled_event(["devin-eligible"]))

    card = service.store.card(25)
    assert card is not None and card.state is State.DEVIN_ELIGIBLE
