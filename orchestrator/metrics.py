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
    autonomy_rate: float | None
    acu_per_merged_pr: float | None
    ci_rounds_to_green: float | None
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
            autonomy_rate=headline["autonomy_rate"],
            acu_per_merged_pr=headline["acu_per_merged_pr"],
            ci_rounds_to_green=headline["ci_rounds_to_green"],
            open_sessions=sum(1 for session in sessions if not session.terminal),
            total_acu=headline["total_acu"],
        )
        self.samples.append(sample)
        return sample

    def series(self) -> list[dict[str, Any]]:
        return [sample.__dict__ for sample in self.samples]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


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

    # Autonomy: a merged PR that took zero human turns. Not "no human looked at
    # it" — review is a human turn we want; this counts interventions that the
    # pipeline needed to make progress.
    autonomous = [
        card for card in merged if card.meta.human_turns == 0 and not card.meta.escalation
    ]
    autonomy_rate = len(autonomous) / len(merged) if merged else None

    total_acu = sum(session.acus_consumed for session in pipeline_sessions)
    merged_acu = sum(card.meta.acus for card in merged)
    acu_per_merged_pr = merged_acu / len(merged) if merged else None

    ci_rounds_to_green = _mean(
        [
            float(card.meta.ci_rounds)
            for card in cards
            if card.state in (State.HUMAN_REVIEW, State.DONE) and card.meta.pr_url
        ]
    )

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

    escalation_counts = Counter(escalations or {})
    for card in cards:
        if card.meta.escalation:
            escalation_counts.setdefault(card.meta.escalation, 0)

    return {
        "headline": {
            "autonomy_rate": autonomy_rate,
            "acu_per_merged_pr": acu_per_merged_pr,
            "ci_rounds_to_green": ci_rounds_to_green,
            "total_acu": round(total_acu, 2),
            "build_acu": round(sum(s.acus_consumed for s in sessions if BUILD_TAG in s.tags), 2),
            "merged": len(merged),
            "retired": len(retired),
        },
        "funnel": funnel,
        "by_tier": by_tier,
        "escalations": dict(escalation_counts),
        "sessions": {
            "active": len([s for s in pipeline_sessions if not s.terminal]),
            "by_role": dict(Counter(s.role for s in pipeline_sessions)),
        },
    }
