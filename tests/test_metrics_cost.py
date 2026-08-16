"""Cost per outcome, and where the outcome count comes from."""

from __future__ import annotations

from orchestrator.github import _filed_at, _to_issue
from orchestrator.labels import State
from orchestrator.metrics import BUILD_TAG, compute
from orchestrator.models import CheckRun, IssueCard, IssueMeta, SessionInfo


def card(number: int, **kwargs: object) -> IssueCard:
    payload: dict[str, object] = {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://example.invalid/{number}",
    }
    payload.update(kwargs)
    return IssueCard.model_validate(payload)


def green() -> list[CheckRun]:
    return [CheckRun(name="ci", status="completed", conclusion="success")]


def red() -> list[CheckRun]:
    return [CheckRun(name="ci", status="completed", conclusion="failure")]


def pending() -> list[CheckRun]:
    return [CheckRun(name="ci", status="in_progress", conclusion=None)]


def session(acus: float, *, build: bool = False) -> SessionInfo:
    return SessionInfo(
        session_id=f"s-{acus}-{build}",
        acus_consumed=acus,
        tags=[BUILD_TAG] if build else ["role:worker"],
    )


def test_acu_per_ready_pr_divides_all_pipeline_spend_by_the_green_prs() -> None:
    cards = [
        card(1, meta=IssueMeta(pr_url="p/1"), checks=green()),
        card(2, meta=IssueMeta(pr_url="p/2"), checks=green()),
        card(3, meta=IssueMeta(pr_url="p/3"), checks=red()),
        card(4),  # declined, no PR, but its triage cost is in the numerator
    ]
    headline = compute(cards, [session(6.0), session(2.0)])["headline"]
    assert headline["ready_pr_count"] == 2
    assert headline["acu_per_ready_pr"] == 4.0


def test_building_cgsol_is_not_charged_to_the_pipeline() -> None:
    cards = [card(1, meta=IssueMeta(pr_url="p/1"), checks=green())]
    headline = compute(cards, [session(3.0), session(100.0, build=True)])["headline"]
    assert headline["acu_per_ready_pr"] == 3.0


def test_spend_survives_the_session_being_archived() -> None:
    """Terminating a run drops it out of the session list. The issue's metadata
    comment is the durable record, so the total does not fall back to zero."""
    meta = IssueMeta(pr_url="p/1")
    meta.record_spend("gone-1", 4.0)
    meta.record_spend("gone-2", 2.0)
    headline = compute([card(1, meta=meta, checks=green())], [])["headline"]
    assert headline["total_acu"] == 6.0
    assert headline["acu_per_ready_pr"] == 6.0


def test_a_live_session_is_not_counted_twice_over_its_recorded_share() -> None:
    meta = IssueMeta(pr_url="p/1")
    meta.record_spend("s-live", 1.0)  # stale: the session now reports more
    live = SessionInfo(session_id="s-live", acus_consumed=3.0, tags=["role:worker"])
    headline = compute([card(1, meta=meta, checks=green())], [live])["headline"]
    assert headline["total_acu"] == 3.0


def test_a_pre_breakdown_metadata_comment_keeps_its_total() -> None:
    meta = IssueMeta.model_validate({"pr_url": "p/1", "acus": 2.5})
    assert meta.acus == 2.5


def test_a_pending_check_is_not_a_ready_pr() -> None:
    cards = [card(1, meta=IssueMeta(pr_url="p/1"), checks=pending())]
    headline = compute(cards, [session(3.0)])["headline"]
    assert headline["ready_pr_count"] == 0
    assert headline["acu_per_ready_pr"] is None


def test_a_merged_pr_counts_even_though_its_checks_are_no_longer_cached() -> None:
    cards = [card(1, meta=IssueMeta(pr_url="p/1"), pr_merged=True)]
    assert compute(cards, [session(3.0)])["headline"]["ready_pr_count"] == 1


def test_an_escalated_card_with_green_ci_still_counts() -> None:
    """Readiness here is the user's definition — CI succeeding. The board's
    `ready-to-merge` label is stricter; the cost metric deliberately is not,
    because the ACUs bought a green PR either way."""
    cards = [
        card(
            1,
            state=State.HUMAN_REVIEW,
            meta=IssueMeta(pr_url="p/1", escalation="ci-unfixable"),
            checks=green(),
        )
    ]
    assert compute(cards, [session(3.0)])["headline"]["ready_pr_count"] == 1


def test_the_import_footer_gives_the_upstream_filing_date() -> None:
    body = (
        "Repro steps.\n\n---\n"
        "_Imported from apache/superset issue 36406. Originally filed 2025-12-03._"
    )
    assert _filed_at(body) == "2025-12-03T00:00:00Z"
    assert _to_issue({"number": 1, "body": body}).filed_at == "2025-12-03T00:00:00Z"


def test_an_issue_without_a_footer_has_no_filing_date() -> None:
    assert _filed_at("just a bug report") == ""
    assert _filed_at("Originally filed yesterday") == ""
    assert _to_issue({"number": 1}).filed_at == ""
