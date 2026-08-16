"""Where a verdict lands, and when a session's result is safe to consume.

Both of these are places where the pipeline decides something on an agent's
say-so, so both are worth pinning down.
"""

from __future__ import annotations

import time
from typing import Any

from orchestrator.config import Settings
from orchestrator.dispatch import Dispatcher
from orchestrator.labels import State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import Issue, SessionInfo, Verdict
from orchestrator.poller import Poller
from orchestrator.state import Store


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.states: list[State] = []

    async def set_state(self, number: int, target: State, current_labels: list[str]) -> None:
        self.states.append(target)

    async def add_labels(self, number: int, labels: list[str]) -> None:
        pass

    async def upsert_meta(self, number: int, meta: Any, note: str = "") -> None:
        self.comments.append(note)


def dispatcher_for(store: Store, github: FakeGitHub) -> Dispatcher:
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.settings = Settings(replay=True)
    dispatcher.store = store
    dispatcher.github = github  # type: ignore[assignment]
    dispatcher.metrics = MetricsRegistry()
    return dispatcher


def store_with_issue() -> Store:
    store = Store()
    store.upsert_issue(
        Issue.model_validate(
            {"number": 7, "title": "stale bug", "labels": ["needs-triage"], "state": "open"}
        )
    )
    return store


def verdict(**kwargs: Any) -> Verdict:
    payload: dict[str, Any] = {
        "issue_number": 7,
        "eligible": False,
        "confidence": 0.15,
        "tier": "none",
        "decline_reason": "already-fixed",
        "reasoning": "Read iran.geojson on master: 31 features, no duplicate codes.",
    }
    payload.update(kwargs)
    return Verdict.model_validate(payload)


# --- retirement ---------------------------------------------------------------


async def test_an_evidenced_already_fixed_verdict_retires_the_issue() -> None:
    store, github = store_with_issue(), FakeGitHub()
    await dispatcher_for(store, github).apply_verdict(verdict())
    card = store.card(7)
    assert card is not None and card.state is State.CAN_CLOSE_ISSUE
    assert "Evidence" in github.comments[0]


async def test_a_duplicate_claim_without_evidence_is_only_a_decline() -> None:
    """Closing a live bug on an unsupported claim is the failure that matters."""
    store, github = store_with_issue(), FakeGitHub()
    await dispatcher_for(store, github).apply_verdict(
        verdict(decline_reason="duplicate", reasoning="   ")
    )
    card = store.card(7)
    assert card is not None and card.state is State.DEVIN_DECLINED


async def test_an_ordinary_decline_still_declines() -> None:
    store, github = store_with_issue(), FakeGitHub()
    await dispatcher_for(store, github).apply_verdict(verdict(decline_reason="product-decision"))
    card = store.card(7)
    assert card is not None and card.state is State.DEVIN_DECLINED


# --- consuming a session's result ---------------------------------------------


def scout_session(status_enum: str, output: dict[str, Any] | None) -> SessionInfo:
    return SessionInfo(
        session_id="devin-1",
        tags=["cgsol", "role:scout"],
        status="running",
        status_enum=status_enum,
        structured_output=output,
    )


class FakeDevin:
    def __init__(self, detail: SessionInfo) -> None:
        self.detail = detail
        self.gets = 0
        self.terminated: list[str] = []

    async def list_by_tags(self, tags: list[str]) -> list[SessionInfo]:
        return [self.detail.model_copy(update={"structured_output": None})]

    async def get_session(self, session_id: str) -> SessionInfo:
        self.gets += 1
        return self.detail

    async def terminate(self, session_id: str) -> bool:
        self.terminated.append(session_id)
        return True


def poller_for(devin: FakeDevin, store: Store, dispatcher: Dispatcher) -> Poller:
    poller = Poller.__new__(Poller)
    poller.settings = Settings(replay=True)
    poller.devin = devin  # type: ignore[assignment]
    poller.store = store
    poller.dispatcher = dispatcher
    poller.metrics = MetricsRegistry()
    poller._consumed = set()
    poller._swept_through = None
    poller._last_full_sweep = 0.0
    return poller


async def test_a_waiting_scout_holding_verdicts_is_not_left_holding_them() -> None:
    """Devin idles at `waiting_for_user`; only sleep produces `finished`."""
    store, github = store_with_issue(), FakeGitHub()
    devin = FakeDevin(
        scout_session("waiting_for_user", {"verdicts": [verdict().model_dump(mode="json")]})
    )
    poller = poller_for(devin, store, dispatcher_for(store, github))

    await poller.poll_sessions()
    card = store.card(7)
    assert card is not None and card.state is State.CAN_CLOSE_ISSUE

    await poller.poll_sessions()
    assert devin.gets == 1  # consumed once, then never fetched again
    # and closed, rather than left waiting for a reply from a state machine
    assert devin.terminated == ["devin-1"]


async def test_a_half_written_batch_is_applied_but_not_written_off() -> None:
    """Structured output is readable while the session is still writing it. The
    verdicts present are worth applying; the ones missing are not lost by
    calling the session consumed."""
    store, github = store_with_issue(), FakeGitHub()
    store.upsert_issue(
        Issue.model_validate(
            {"number": 8, "title": "second", "labels": ["needs-triage"], "state": "open"}
        )
    )
    session = scout_session("waiting_for_user", {"verdicts": [verdict().model_dump(mode="json")]})
    devin = FakeDevin(session.model_copy(update={"tags": [*session.tags, "issue:7", "issue:8"]}))
    poller = poller_for(devin, store, dispatcher_for(store, github))

    await poller.poll_sessions()
    seven, eight = store.card(7), store.card(8)
    assert seven is not None and seven.state is State.CAN_CLOSE_ISSUE
    assert eight is not None and eight.state is State.NEEDS_TRIAGE

    await poller.poll_sessions()
    assert devin.gets == 2  # still being polled: #8's verdict may yet arrive
    assert devin.terminated == []  # and not closed out from under the batch


async def test_an_eligible_verdict_goes_to_a_worker_not_a_human_by_default() -> None:
    """The analyst's eligibility call is the gate; confidence only informs it."""
    store, github = store_with_issue(), FakeGitHub()
    await dispatcher_for(store, github).apply_verdict(
        verdict(eligible=True, tier="hard", decline_reason="none", confidence=0.55)
    )
    card = store.card(7)
    assert card is not None and card.state is State.DEVIN_ELIGIBLE


async def test_raising_the_threshold_puts_a_hesitant_verdict_in_front_of_a_human() -> None:
    store, github = store_with_issue(), FakeGitHub()
    dispatcher = dispatcher_for(store, github)
    dispatcher.settings = Settings(replay=True, confidence_threshold=0.6)
    await dispatcher.apply_verdict(
        verdict(eligible=True, tier="hard", decline_reason="none", confidence=0.55)
    )
    card = store.card(7)
    assert card is not None and card.state is State.HUMAN_REVIEW


async def test_an_automation_session_is_adopted_even_if_it_is_already_paused() -> None:
    """First sight and stopped are not exclusive, and there is no second first sight."""
    store, github = store_with_issue(), FakeGitHub()
    session = SessionInfo(
        session_id="devin-1",
        tags=["cgsol", "role:worker", "issue:7"],
        status="running",
        status_enum="waiting_for_user",
        origin="automation",
    )
    poller = poller_for(FakeDevin(session), store, dispatcher_for(store, github))

    await poller.poll_sessions()
    card = store.card(7)
    assert card is not None and card.meta.session_id == "devin-1"


class ReconcileGitHub(FakeGitHub):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__()
        self.issues = issues

    def server_time(self) -> float:
        return time.time()

    @property
    def budget(self) -> dict[str, Any]:
        return {}

    async def list_issues(
        self, state: str = "all", limit: int = 100, since: str | None = None
    ) -> list[Issue]:
        return self.issues

    async def list_open_prs(self) -> list[dict[str, Any]]:
        return []

    async def find_meta_comment(self, number: int) -> None:
        return None


async def test_closing_an_issue_the_pipeline_never_touched_is_not_a_merge() -> None:
    """A maintainer clearing their own backlog must not read as pipeline output."""
    store = Store()
    issue = Issue.model_validate(
        {"number": 2, "title": "closed by hand", "labels": [], "state": "closed"}
    )
    github = ReconcileGitHub([issue])
    devin = FakeDevin(scout_session("finished", None))
    poller = poller_for(devin, store, dispatcher_for(store, github))
    poller.github = github  # type: ignore[assignment]

    await poller.reconcile()
    card = store.card(2)
    assert card is not None and card.state is None


async def test_a_waiting_scout_with_nothing_to_say_is_retried_not_written_off() -> None:
    """Waiting is also how a session asks a question mid-flight."""
    store, github = store_with_issue(), FakeGitHub()
    devin = FakeDevin(scout_session("waiting_for_user", None))
    poller = poller_for(devin, store, dispatcher_for(store, github))

    await poller.poll_sessions()
    await poller.poll_sessions()
    assert devin.gets == 2
    card = store.card(7)
    assert card is not None and card.state is State.NEEDS_TRIAGE
