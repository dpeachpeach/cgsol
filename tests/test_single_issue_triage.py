"""Triaging one card from its drawer, rather than the whole backlog.

The button exists because a maintainer looking at one issue should not have to
spend a scout on forty-five to get a verdict on it. It routes through the same
debounced batch as a webhook, so a card triaged by hand and a card triaged by
the pipeline cannot end up in different states.
"""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.labels import State
from orchestrator.models import Issue
from orchestrator.service import Orchestrator
from orchestrator.state import Store
from orchestrator.webhooks import Debouncer


def issue(number: int, labels: list[str]) -> Issue:
    return Issue.model_validate(
        {"number": number, "title": f"#{number}", "labels": labels, "state": "open"}
    )


def service_with(batches: list[set[int]]) -> Orchestrator:
    service = Orchestrator.__new__(Orchestrator)
    service.settings = Settings(replay=True)
    service.store = Store()

    async def flush(numbers: set[int]) -> None:
        batches.append(set(numbers))

    service.debouncer = Debouncer(60.0, flush)
    return service


async def test_one_card_reaches_a_scout_without_waiting_out_the_window() -> None:
    batches: list[set[int]] = []
    service = service_with(batches)

    result = await service.triage_issue(24)

    assert result == {"queued": [24]}
    assert batches == [{24}]


async def test_anything_already_pending_rides_along_in_the_same_scout() -> None:
    """Two scouts for two issues is the expensive way to answer one question."""
    batches: list[set[int]] = []
    service = service_with(batches)

    await service.debouncer.add(9)
    result = await service.triage_issue(24)

    assert result == {"queued": [9, 24]}
    assert batches == [{9, 24}]


async def test_the_button_only_offers_itself_for_untriaged_cards() -> None:
    """The drawer hides it once a card has a verdict; the projection is what it
    reads, so an eligible card must not look untriaged."""
    service = service_with([])
    service.store.upsert_issue(issue(1, []))
    service.store.upsert_issue(issue(2, [State.NEEDS_TRIAGE.value]))
    service.store.upsert_issue(issue(3, [State.DEVIN_ELIGIBLE.value]))

    states = {card.number: card.state for card in service.store.cards()}
    assert states == {1: None, 2: State.NEEDS_TRIAGE, 3: State.DEVIN_ELIGIBLE}
