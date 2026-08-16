"""Metadata blob, prefilter, and the projection — the parts that decide what
gets a session and what it costs."""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.dispatch import Dispatcher
from orchestrator.github import parse_meta, render_meta
from orchestrator.labels import State, Tier
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import Issue, IssueMeta, SessionInfo
from orchestrator.state import Store


def issue(number: int, **kwargs: object) -> Issue:
    payload: dict[str, object] = {
        "number": number,
        "title": f"issue {number}",
        "labels": [],
        "state": "open",
    }
    payload.update(kwargs)
    return Issue.model_validate(payload)


def prefilter(issues: list[Issue]) -> list[int]:
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.settings = Settings(replay=True)
    return [i.number for i in dispatcher.pre_filter(issues)]


# --- metadata blob ------------------------------------------------------------


def test_meta_survives_a_round_trip_through_an_html_comment() -> None:
    meta = IssueMeta(session_id="devin-abc", tier="medium", attempt=2, ci_rounds=1, confidence=0.84)
    body = f"Some human text.\n\n{render_meta(meta)}\n"
    assert parse_meta(body) == meta


def test_meta_is_absent_rather_than_wrong_when_the_comment_is_broken() -> None:
    assert parse_meta("no blob here") is None
    assert parse_meta("<!-- devin-orchestrator: {not json} -->") is None


# --- prefilter ----------------------------------------------------------------


def test_prefilter_drops_what_costs_nothing_to_decide() -> None:
    kept = prefilter(
        [
            issue(1),
            issue(2, state="closed"),
            issue(3, has_linked_pr=True),
            issue(4, labels=["devin-working"]),
            issue(5, labels=["needs-triage"]),
        ]
    )
    assert kept == [1, 5]


def test_prefilter_deduplicates_titles_ignoring_punctuation() -> None:
    kept = prefilter(
        [
            issue(1, title="Bump lodash to 4.17.21"),
            issue(2, title="bump lodash to 4.17.21!"),
        ]
    )
    assert kept == [1]


def test_estimate_quotes_the_bill_before_spending_it() -> None:
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.settings = Settings(replay=True)
    estimate = dispatcher.estimate([issue(n) for n in range(1, 19)])
    assert estimate.issue_count == 18
    assert 0 < estimate.estimated_acu <= dispatcher.settings.acu_ceiling_scout


# --- projection ---------------------------------------------------------------


def test_session_joins_to_its_issue_through_tags_alone() -> None:
    """The link is written on both sides so either one can rebuild it."""
    store = Store()
    store.upsert_issue(issue(7, labels=["devin-working", "tier:hard"]))
    store.upsert_session(
        SessionInfo(
            session_id="devin-1",
            tags=["cgsol", "role:worker", "issue:7"],
            acus_consumed=1.25,
            status="running",
            status_enum="working",
        )
    )
    card = store.card(7)
    assert card is not None
    assert card.state is State.DEVIN_WORKING
    assert card.tier is Tier.HARD
    assert card.session is not None
    assert card.meta.acus == 1.25
    assert store.active_worker_count() == 1


def test_terminal_sessions_stop_counting_as_active() -> None:
    store = Store()
    store.upsert_session(
        SessionInfo(
            session_id="devin-1",
            tags=["role:worker", "issue:7"],
            status="finished",
            status_enum="finished",
        )
    )
    assert store.active_worker_count() == 0
    assert store.sessions()[0].terminal


def test_metrics_ring_buffer_is_bounded() -> None:
    registry = MetricsRegistry(capacity=3)
    for _ in range(10):
        registry.sample([], [])
    assert len(registry.series()) == 3
