"""The playbooks are the product. These assert the properties they are relied
on for, so a well-meaning edit cannot quietly remove them."""

from __future__ import annotations

import pytest

from orchestrator.automations import load_automations, render
from orchestrator.config import Settings
from orchestrator.resources import load_resources

WORKER_KEYS = ["remediate_trivial", "remediate_medium", "remediate_hard"]

CEILINGS = {
    "triage_scout": 3.0,
    "remediate_trivial": 1.5,
    "remediate_medium": 3.0,
    "remediate_hard": 5.0,
    "ci_autofix": 2.0,
}


@pytest.fixture(scope="module")
def resources():  # type: ignore[no-untyped-def]
    return load_resources()


def test_every_playbook_the_dispatcher_asks_for_exists(resources) -> None:  # type: ignore[no-untyped-def]
    assert set(CEILINGS) <= set(resources.playbooks)
    assert resources.knowledge


@pytest.mark.parametrize(("key", "ceiling"), CEILINGS.items())
def test_ceilings_are_containment_not_thrift(resources, key: str, ceiling: float) -> None:  # type: ignore[no-untyped-def]
    assert resources.playbooks[key].max_acu_limit == ceiling


def test_scout_is_read_only_and_must_emit_structured_output(resources) -> None:  # type: ignore[no-untyped-def]
    scout = resources.playbooks["triage_scout"]
    assert scout.structured_output_required
    schema = scout.structured_output_schema or {}
    verdict = schema["properties"]["verdicts"]["items"]["properties"]
    assert {"issue_number", "eligible", "confidence", "tier", "blast_radius", "reasoning"} <= set(
        verdict
    )
    body = scout.body.lower()
    assert "do not" in body
    assert "read-only" in body or "read only" in body


@pytest.mark.parametrize("key", WORKER_KEYS)
def test_workers_are_told_not_to_build_an_environment(resources, key: str) -> None:  # type: ignore[no-untyped-def]
    """Left alone Devin will `npm ci`, because that is what a careful engineer
    does. CI is the gate; the session is not."""
    body = resources.playbooks[key].body.lower()
    assert "do not set up the development environment" in body
    assert "do not run the test suite" in body


def test_ci_autofix_stops_instead_of_looping(resources) -> None:  # type: ignore[no-untyped-def]
    spec = resources.playbooks["ci_autofix"]
    body = spec.body.lower()
    assert spec.max_attempts == 3
    assert "three" in body
    assert "goes to a human" in body


def test_automations_render_with_the_repo_substituted() -> None:
    automations = load_automations()
    keys = {automation["key"] for automation in automations}
    assert {"ci_autofix", "pr_review", "dependency_scan"} <= keys
    text = "\n".join(render(automation, "dpeachpeach/superset-cg") for automation in automations)
    assert "${GITHUB_REPO}" not in text
    assert "dpeachpeach/superset-cg" in text


def test_ci_autofix_automation_writes_the_state_machine_itself() -> None:
    """Devin is a peer on the bus, not a subordinate reporting through us."""
    automation = next(a for a in load_automations() if a["key"] == "ci_autofix")
    prompt = automation["prompt"].lower()
    assert "ci-failing" in prompt
    assert "human-review" in prompt
    assert automation["max_acu_limit"] == 2


def test_replay_settings_need_no_credentials() -> None:
    settings = Settings(replay=True, github_token="", devin_api_key="", devin_org_id="")
    assert settings.devin_org_id
    assert settings.github_token
