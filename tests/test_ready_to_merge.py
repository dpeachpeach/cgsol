"""The `ready-to-merge` label on a worker's pull request.

The claim it makes is narrow and worth keeping narrow: the gate has passed and
nothing is left but a person's judgement. It is derived from the checks the
orchestrator read, never from the worker's own sign-off comment, and it lives
on the PR because that is where the reviewer is.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings
from orchestrator.dispatch import Dispatcher
from orchestrator.labels import READY_TO_MERGE_LABEL, EscalationReason, State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import CheckRun, Issue, IssueCard
from orchestrator.state import Store


class FakeGitHub:
    def __init__(self) -> None:
        self.added: list[tuple[int, list[str]]] = []
        self.removed: list[tuple[int, str]] = []

    async def set_state(self, number: int, target: State, current_labels: list[str]) -> None:
        pass

    async def add_labels(self, number: int, labels: list[str]) -> None:
        self.added.append((number, labels))

    async def remove_label(self, number: int, label: str) -> None:
        self.removed.append((number, label))

    async def upsert_meta(self, number: int, meta: Any, note: str = "") -> None:
        pass


def setup(state: State, escalation: str | None = None) -> tuple[IssueCard, FakeGitHub, Dispatcher]:
    store = Store()
    store.upsert_issue(
        Issue.model_validate(
            {"number": 7, "title": "bug", "labels": [state.value], "state": "open"}
        )
    )
    card = store.card(7)
    assert card is not None
    card.pr_number = 101
    card.meta.escalation = escalation

    github = FakeGitHub()
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.settings = Settings(replay=True, max_concurrent_workers=0)
    dispatcher.store = store
    dispatcher.github = github  # type: ignore[assignment]
    dispatcher.metrics = MetricsRegistry()
    return card, github, dispatcher


GREEN = [CheckRun(name="pre-commit", status="completed", conclusion="success")]
RED = [CheckRun(name="pre-commit", status="completed", conclusion="failure")]
PENDING = [CheckRun(name="pre-commit", status="in_progress")]


async def test_a_green_pr_that_reaches_human_review_is_labelled() -> None:
    card, github, dispatcher = setup(State.DEVIN_PR_OPEN)

    await dispatcher.evaluate_ci(card, GREEN, False)

    assert card.state is State.HUMAN_REVIEW and card.ready_to_merge
    assert github.added == [(101, [READY_TO_MERGE_LABEL])]  # the PR, not the issue


async def test_a_card_already_waiting_on_review_is_labelled_too() -> None:
    """The usual case after a restart: the transition happened in a previous
    process, and the first read of the checks is the only chance to say so."""
    card, github, dispatcher = setup(State.HUMAN_REVIEW)

    await dispatcher.evaluate_ci(card, GREEN, False)

    assert github.added == [(101, [READY_TO_MERGE_LABEL])]


async def test_the_label_is_written_once() -> None:
    card, github, dispatcher = setup(State.HUMAN_REVIEW)

    await dispatcher.evaluate_ci(card, GREEN, False)
    await dispatcher.evaluate_ci(card, GREEN, False)

    assert len(github.added) == 1


async def test_checks_still_running_are_not_a_pass() -> None:
    card, github, dispatcher = setup(State.HUMAN_REVIEW)

    await dispatcher.evaluate_ci(card, PENDING, False)

    assert github.added == []


async def test_an_escalation_that_is_not_low_confidence_keeps_the_human() -> None:
    """`ci-unfixable` and friends are in human-review because the pipeline does
    not trust the change; a green check does not answer the question they ask."""
    card, github, dispatcher = setup(State.HUMAN_REVIEW, EscalationReason.CI_UNFIXABLE.value)

    await dispatcher.evaluate_ci(card, GREEN, False)

    assert github.added == []


async def test_low_confidence_alone_is_still_ready() -> None:
    card, github, dispatcher = setup(State.HUMAN_REVIEW, EscalationReason.LOW_CONFIDENCE.value)

    await dispatcher.evaluate_ci(card, GREEN, False)

    assert github.added == [(101, [READY_TO_MERGE_LABEL])]


async def test_the_label_comes_off_when_ci_goes_red() -> None:
    card, github, dispatcher = setup(State.HUMAN_REVIEW)
    await dispatcher.evaluate_ci(card, GREEN, False)

    await dispatcher.evaluate_ci(card, RED, False)

    assert not card.ready_to_merge
    assert github.removed == [(101, READY_TO_MERGE_LABEL)]


async def test_a_card_with_no_pr_is_never_labelled() -> None:
    card, github, dispatcher = setup(State.HUMAN_REVIEW)
    card.pr_number = None

    await dispatcher.evaluate_ci(card, GREEN, False)

    assert github.added == []
