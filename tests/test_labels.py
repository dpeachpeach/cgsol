from __future__ import annotations

from orchestrator.labels import (
    ALL_STATE_LABELS,
    EscalationReason,
    State,
    Tier,
    all_states_on,
    can_transition,
    state_of,
    tier_of,
)
from orchestrator.resources import load_label_definitions


def test_forward_transitions_are_legal_and_backwards_ones_are_not() -> None:
    assert can_transition(State.NEEDS_TRIAGE, State.DEVIN_ELIGIBLE)
    assert can_transition(State.DEVIN_PR_OPEN, State.CI_FAILING)
    assert can_transition(State.CI_FAILING, State.DEVIN_FIXING)
    assert not can_transition(State.DONE, State.DEVIN_WORKING)
    assert not can_transition(State.DEVIN_PR_OPEN, State.NEEDS_TRIAGE)


def test_repeating_the_current_state_is_a_no_op_not_an_error() -> None:
    """Three actors write labels; the same target arriving twice must be safe."""
    assert can_transition(State.DEVIN_WORKING, State.DEVIN_WORKING)
    assert can_transition(None, State.NEEDS_TRIAGE)


def test_two_state_labels_read_as_unknown() -> None:
    assert state_of(["needs-triage", "bug"]) is State.NEEDS_TRIAGE
    assert state_of(["needs-triage", "devin-working"]) is None
    assert all_states_on(["needs-triage", "devin-working"]) == [
        State.NEEDS_TRIAGE,
        State.DEVIN_WORKING,
    ]


def test_tier_round_trips_through_its_label() -> None:
    assert tier_of(["bug", "tier:medium"]) is Tier.MEDIUM
    assert tier_of(["bug"]) is None
    assert Tier.HARD.label == "tier:hard"
    assert Tier.from_label("tier:nope") is None


def test_every_label_the_code_uses_exists_in_the_corpus_definition() -> None:
    """One definition of the taxonomy, not two: seed/labels.yaml creates them
    and the state machine reads them."""
    defined = {name for name, _, _ in load_label_definitions()}
    assert defined >= ALL_STATE_LABELS
    assert defined >= {tier.label for tier in Tier}
    assert defined >= {reason.label for reason in EscalationReason}
