"""The two clock-based metrics. Both are read off GitHub's own timestamps, so
the interesting cases are the ones where a timestamp is missing or unusable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.labels import State
from orchestrator.metrics import compute
from orchestrator.models import IssueCard, IssueMeta


def stamp(hours_ago: float) -> str:
    return (
        (datetime.now(UTC) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    ).replace("+00:00", "Z")


def card(number: int, **kwargs: object) -> IssueCard:
    payload: dict[str, object] = {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://example.invalid/{number}",
    }
    payload.update(kwargs)
    return IssueCard.model_validate(payload)


def test_issue_to_pr_averages_only_the_issues_that_have_both_timestamps() -> None:
    cards = [
        card(1, created_at=stamp(10), meta=IssueMeta(pr_opened_at=stamp(8))),  # 2h
        card(2, created_at=stamp(10), meta=IssueMeta(pr_opened_at=stamp(6))),  # 4h
        card(3, created_at=stamp(10)),  # no PR
        card(4, meta=IssueMeta(pr_opened_at=stamp(6))),  # no issue timestamp
    ]
    headline = compute(cards, [])["headline"]
    assert headline["issue_to_pr_count"] == 2
    assert headline["issue_to_pr_seconds"] == 3 * 3600


def test_issue_to_pr_is_none_when_nothing_has_reached_a_pr() -> None:
    headline = compute([card(1, created_at=stamp(4))], [])["headline"]
    assert headline["issue_to_pr_seconds"] is None
    assert headline["issue_to_pr_count"] == 0


def test_open_age_counts_open_issues_only() -> None:
    cards = [
        card(1, created_at=stamp(2)),
        card(2, created_at=stamp(4)),
        card(3, created_at=stamp(100), pr_merged=True),
        card(4, created_at=stamp(100), state=State.DONE),
    ]
    headline = compute(cards, [])["headline"]
    assert headline["open_count"] == 2
    assert headline["open_age_seconds"] is not None
    assert abs(headline["open_age_seconds"] - 3 * 3600) < 60


def test_an_unparseable_timestamp_is_a_missing_measurement_not_a_zero() -> None:
    headline = compute([card(1, created_at="not a date")], [])["headline"]
    assert headline["open_count"] == 0
    assert headline["open_age_seconds"] is None
