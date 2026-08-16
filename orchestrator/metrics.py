"""Observability.

The kanban answers "what is happening". These answer "is this working" — which
is a different question, and the only one worth putting in front of someone who
has to decide whether to run it.

Everything except the time-series is derived from the projection on demand, so
metrics cannot disagree with the board. The time-series is the one non-derivable
thing, so it gets a ring buffer (and, optionally, a JSON file committed to the
fork for durable history).
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orchestrator.labels import State, Tier
from orchestrator.models import IssueCard, SessionInfo

#: Sessions that built the system are tagged separately from sessions the
#: pipeline dispatched, so the burn-down is not dominated by "building the thing".
BUILD_TAG = "cgsol-build"
PIPELINE_TAG = "cgsol-pipeline"

FUNNEL_STAGES = [
    "ingested",
    "triaged",
    "eligible",
    "dispatched",
    "pr_opened",
    "ci_green",
    "merged",
]


@dataclass
class Sample:
    ts: float
    acu_per_ready_pr: float | None
    open_sessions: int
    total_acu: float


@dataclass
class MetricsRegistry:
    capacity: int = 2880  # ~24h at one sample per 30s
    samples: deque[Sample] = field(default_factory=deque)
    escalations: Counter[str] = field(default_factory=Counter)
    dispatch_count: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self.samples = deque(self.samples, maxlen=self.capacity)

    def record_escalation(self, reason: str) -> None:
        self.escalations[reason] += 1

    def record_dispatch(self, role: str) -> None:
        self.dispatch_count[role] += 1

    def sample(self, cards: list[IssueCard], sessions: list[SessionInfo]) -> Sample:
        computed = compute(cards, sessions, self.escalations)
        headline = computed["headline"]
        sample = Sample(
            ts=time.time(),
            acu_per_ready_pr=headline["acu_per_ready_pr"],
            open_sessions=sum(1 for session in sessions if not session.terminal),
            total_acu=headline["total_acu"],
        )
        self.samples.append(sample)
        return sample

    def series(self) -> list[dict[str, Any]]:
        return [sample.__dict__ for sample in self.samples]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _epoch(stamp: str | None) -> float | None:
    """GitHub's `2026-08-15T23:48:00Z` as seconds. Anything unparseable is a
    missing measurement rather than a zero, which would read as "instant"."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ci_green(card: IssueCard) -> bool:
    """A PR a maintainer could merge: the checks concluded and none of them
    failed. Read off the checks rather than the card's state, so a merged PR
    still counts and an escalation does not silently un-count one."""
    if not card.meta.pr_url:
        return False
    if card.pr_merged or card.state is State.DONE:
        return True
    if not card.checks:
        return False
    return all(
        check.status == "completed" and check.conclusion in ("success", "neutral", "skipped")
        for check in card.checks
    )


def compute(
    cards: list[IssueCard],
    sessions: list[SessionInfo],
    escalations: Counter[str] | None = None,
) -> dict[str, Any]:
    pipeline_sessions = [s for s in sessions if BUILD_TAG not in s.tags]
    merged = [card for card in cards if card.pr_merged or card.state is State.DONE]
    # Issues taken off the backlog without any code being written. The cheapest
    # output the pipeline has, and invisible if it is filed under "declined".
    retired = [card for card in cards if card.state is State.CAN_CLOSE_ISSUE]
    with_pr = [card for card in cards if card.meta.pr_url]

    total_acu = sum(session.acus_consumed for session in pipeline_sessions)

    # The cost story in one number: everything the pipeline spent, over the
    # things it produced that a maintainer can actually merge. Triage, declines
    # and failed attempts are in the numerator on purpose — they are what a
    # green PR costs, and an average over the winners alone would flatter it.
    ready = [card for card in cards if _ci_green(card)]
    acu_per_ready_pr = total_acu / len(ready) if ready else None

    funnel = {
        "ingested": len(cards),
        "triaged": len([c for c in cards if c.state is not State.NEEDS_TRIAGE]),
        "eligible": len([c for c in cards if c.tier is not None and c.state is not None]),
        "dispatched": len([c for c in cards if c.meta.session_id]),
        "pr_opened": len(with_pr),
        "ci_green": len([c for c in cards if c.state in (State.HUMAN_REVIEW, State.DONE)]),
        "merged": len(merged),
    }

    by_tier: dict[str, dict[str, Any]] = {}
    for tier in Tier:
        tier_cards = [card for card in cards if card.tier is tier]
        tier_merged = [c for c in tier_cards if c.pr_merged or c.state is State.DONE]
        spend = sum(card.meta.acus for card in tier_cards)
        by_tier[tier.value] = {
            "issues": len(tier_cards),
            "acu": round(spend, 2),
            "acu_share": round(spend / total_acu, 3) if total_acu else None,
            "merged": len(tier_merged),
            "merge_rate": round(len(tier_merged) / len(tier_cards), 3) if tier_cards else None,
            "ci_rounds": _mean([float(c.meta.ci_rounds) for c in tier_cards]) or 0.0,
        }

    # Issue opened -> its PR opened, from GitHub's own timestamps rather than
    # from dispatch, so the figure survives a lost session and a restart.
    issue_to_pr: list[float] = []
    open_ages: list[float] = []
    now = time.time()
    for card in cards:
        created = _epoch(card.created_at)
        if created is not None:
            opened = _epoch(card.meta.pr_opened_at)
            if opened is not None and opened >= created:
                issue_to_pr.append(opened - created)
        # Age is measured from when the bug was first reported upstream, not
        # from when it was copied onto the fork: the backlog it describes is
        # months old, and the fork's own clock would report hours.
        filed = _epoch(card.filed_at) or created
        if filed is not None and not card.pr_merged and card.state is not State.DONE:
            open_ages.append(now - filed)

    escalation_counts = Counter(escalations or {})
    for card in cards:
        if card.meta.escalation:
            escalation_counts.setdefault(card.meta.escalation, 0)

    return {
        "headline": {
            "acu_per_ready_pr": acu_per_ready_pr,
            "ready_pr_count": len(ready),
            "total_acu": round(total_acu, 2),
            "build_acu": round(sum(s.acus_consumed for s in sessions if BUILD_TAG in s.tags), 2),
            "merged": len(merged),
            "retired": len(retired),
            "issue_to_pr_seconds": _mean(issue_to_pr),
            "issue_to_pr_count": len(issue_to_pr),
            "open_age_seconds": _mean(open_ages),
            "open_count": len(open_ages),
        },
        "funnel": funnel,
        "by_tier": by_tier,
        "escalations": dict(escalation_counts),
        "sessions": {
            "active": len([s for s in pipeline_sessions if not s.terminal]),
            "by_role": dict(Counter(s.role for s in pipeline_sessions)),
        },
    }
