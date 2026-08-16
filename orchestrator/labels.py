"""The label taxonomy is the state machine. GitHub is the database."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    NEEDS_TRIAGE = "needs-triage"
    DEVIN_ELIGIBLE = "devin-eligible"
    DEVIN_WORKING = "devin-working"
    DEVIN_PR_OPEN = "devin-pr-open"
    CI_FAILING = "ci-failing"
    DEVIN_FIXING = "devin-fixing"
    HUMAN_REVIEW = "human-review"
    DEVIN_DECLINED = "devin-declined"
    CAN_CLOSE_ISSUE = "can-close-issue"
    DEVIN_BLOCKED = "devin-blocked"
    DONE = "done"


class Tier(str, Enum):
    TRIVIAL = "trivial"
    MEDIUM = "medium"
    HARD = "hard"

    @property
    def label(self) -> str:
        return f"tier:{self.value}"

    @classmethod
    def from_label(cls, label: str) -> Tier | None:
        if not label.startswith("tier:"):
            return None
        try:
            return cls(label.split(":", 1)[1])
        except ValueError:
            return None


ALL_STATE_LABELS: frozenset[str] = frozenset(state.value for state in State)

TERMINAL_STATES: frozenset[State] = frozenset(
    {State.DONE, State.DEVIN_DECLINED, State.CAN_CLOSE_ISSUE}
)

#: Decline reasons that mean "there is no work here" rather than "an agent should
#: not do this work". They retire an issue instead of shelving it: the cheapest
#: thing this pipeline produces is a backlog that shrank without anyone writing
#: code. A human still does the closing.
RETIREMENT_REASONS: frozenset[str] = frozenset({"already-fixed", "duplicate"})

#: States the orchestrator may move an issue *out of*. Anything else is either
#: terminal or belongs to a human.
ORCHESTRATOR_OWNED: frozenset[State] = frozenset(
    {
        State.NEEDS_TRIAGE,
        State.DEVIN_ELIGIBLE,
        State.DEVIN_WORKING,
        State.DEVIN_PR_OPEN,
        State.CI_FAILING,
        State.DEVIN_FIXING,
    }
)

#: Legal transitions. Enforced so a webhook race cannot walk an issue backwards.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.NEEDS_TRIAGE: frozenset(
        {
            State.DEVIN_ELIGIBLE,
            State.DEVIN_DECLINED,
            State.CAN_CLOSE_ISSUE,
            State.HUMAN_REVIEW,
            State.DEVIN_BLOCKED,
        }
    ),
    State.DEVIN_ELIGIBLE: frozenset(
        {State.DEVIN_WORKING, State.HUMAN_REVIEW, State.DEVIN_BLOCKED, State.NEEDS_TRIAGE}
    ),
    State.DEVIN_WORKING: frozenset(
        {State.DEVIN_PR_OPEN, State.DEVIN_BLOCKED, State.HUMAN_REVIEW, State.DEVIN_ELIGIBLE}
    ),
    State.DEVIN_PR_OPEN: frozenset(
        {State.CI_FAILING, State.HUMAN_REVIEW, State.DONE, State.DEVIN_BLOCKED}
    ),
    State.CI_FAILING: frozenset({State.DEVIN_FIXING, State.HUMAN_REVIEW, State.DEVIN_BLOCKED}),
    State.DEVIN_FIXING: frozenset(
        {State.DEVIN_PR_OPEN, State.CI_FAILING, State.HUMAN_REVIEW, State.DEVIN_BLOCKED}
    ),
    State.HUMAN_REVIEW: frozenset(
        {State.DONE, State.DEVIN_ELIGIBLE, State.NEEDS_TRIAGE, State.DEVIN_BLOCKED}
    ),
    State.DEVIN_BLOCKED: frozenset({State.NEEDS_TRIAGE, State.DEVIN_ELIGIBLE, State.HUMAN_REVIEW}),
    State.DEVIN_DECLINED: frozenset({State.NEEDS_TRIAGE, State.CAN_CLOSE_ISSUE}),
    # A human either closes it, or disagrees and sends it back for work.
    State.CAN_CLOSE_ISSUE: frozenset({State.DONE, State.NEEDS_TRIAGE, State.HUMAN_REVIEW}),
    State.DONE: frozenset(),
}


def can_transition(current: State | None, target: State) -> bool:
    if current is None:
        return True
    if current is target:
        return True
    return target in TRANSITIONS.get(current, frozenset())


def state_of(labels: list[str]) -> State | None:
    """The single state label on an issue, if exactly one is present.

    Multiple state labels means concurrent writers disagreed; the reconciler
    resolves that, callers here just see 'unknown'.
    """
    found = [State(label) for label in labels if label in ALL_STATE_LABELS]
    if len(found) == 1:
        return found[0]
    return None


def all_states_on(labels: list[str]) -> list[State]:
    return [State(label) for label in labels if label in ALL_STATE_LABELS]


def tier_of(labels: list[str]) -> Tier | None:
    for label in labels:
        tier = Tier.from_label(label)
        if tier is not None:
            return tier
    return None


class EscalationReason(str, Enum):
    AMBIGUOUS_REQUIREMENT = "ambiguous-requirement"
    MISSING_CONTEXT = "missing-context"
    LARGER_THAN_TIERED = "larger-than-tiered"
    NEEDS_APPROVAL = "needs-approval"
    CI_UNFIXABLE = "ci-unfixable"
    ACU_EXHAUSTED = "acu-exhausted"
    SESSION_ERROR = "session-error"
    LOW_CONFIDENCE = "low-confidence"

    @property
    def label(self) -> str:
        return f"escalation:{self.value}"


#: Colours and descriptions live in seed/labels.yaml, next to the corpus that
#: needs them; see `orchestrator.resources.load_label_definitions`.
