"""Eligible, and nothing has started on it yet.

Triage finishing is not progress a maintainer can see: `devin-eligible` looked
identical to an untriaged issue sitting in the backlog, and with the spend cap
at zero it could sit there indefinitely with nothing on the board saying why.
The status is derived rather than labelled, because what it describes is this
process's capacity, not the issue.
"""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.dispatch import Dispatcher
from orchestrator.labels import State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import Issue, SessionInfo
from orchestrator.state import Store


def card_for(state: State) -> tuple[Store, int]:
    store = Store()
    store.upsert_issue(
        Issue.model_validate(
            {"number": 7, "title": "unpinned dependency", "labels": [state.value], "state": "open"}
        )
    )
    return store, 7


def worker(status: str = "running") -> SessionInfo:
    return SessionInfo.model_validate(
        {
            "session_id": "devin-1",
            "status": status,
            "tags": ["cgsol", "role:worker", "issue:7"],
            "url": "https://app.devin.ai/sessions/devin-1",
        }
    )


def test_eligible_with_no_session_is_awaiting_pickup() -> None:
    store, number = card_for(State.DEVIN_ELIGIBLE)
    card = store.card(number)
    assert card is not None and card.pickup_status == "awaiting-devin"


def test_a_live_worker_clears_it() -> None:
    store, number = card_for(State.DEVIN_ELIGIBLE)
    store.upsert_session(worker())
    card = store.card(number)
    assert card is not None and card.pickup_status is None


def test_a_finished_worker_leaves_the_card_waiting_again() -> None:
    """A session that exited without moving the card is not work in progress."""
    store, number = card_for(State.DEVIN_ELIGIBLE)
    store.upsert_session(worker(status="exit"))
    card = store.card(number)
    assert card is not None and card.pickup_status == "awaiting-devin"


def test_every_other_state_has_no_pickup_status() -> None:
    for state in State:
        if state is State.DEVIN_ELIGIBLE:
            continue
        store, number = card_for(state)
        card = store.card(number)
        assert card is not None and card.pickup_status is None


def test_it_is_projection_only_and_never_reaches_github() -> None:
    """The frontend reads it off the card; the metadata comment must not carry
    it, or a capacity detail becomes part of the durable record."""
    store, number = card_for(State.DEVIN_ELIGIBLE)
    card = store.card(number)
    assert card is not None
    assert "pickup_status" in card.model_dump(mode="json")
    assert "pickup_status" not in card.meta.model_dump(mode="json")


async def test_the_cap_at_zero_leaves_it_awaiting_rather_than_hiding_it() -> None:
    store, number = card_for(State.DEVIN_ELIGIBLE)
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.settings = Settings(replay=True, max_concurrent_workers=0)
    dispatcher.store = store
    dispatcher.metrics = MetricsRegistry()

    assert await dispatcher.dispatch_ready() == 0
    card = store.card(number)
    assert card is not None
    assert card.state is State.DEVIN_ELIGIBLE and card.pickup_status == "awaiting-devin"
