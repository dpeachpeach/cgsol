"""Replay as the integration suite.

The same scripted run drives the simulated fork and the checked-in cassettes,
and both have to produce the same board. That is what makes the cassettes worth
committing: if the state machine starts routing an issue somewhere else, the
phase table stops matching and CI fails here rather than in the demo.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from orchestrator.labels import State
from orchestrator.metrics import compute
from orchestrator.replay import Phase, replay_run, run_timeline
from orchestrator.simulate import load_deliveries, sign
from orchestrator.transport import CassetteMissError, ReplayTransport
from orchestrator.webhooks import is_bot_sender, verify_signature

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TIMELINE = json.loads((FIXTURES / "timeline.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def simulated() -> list[Phase]:
    import asyncio

    async def run() -> list[Phase]:
        async with replay_run() as orchestrator:
            return await run_timeline(orchestrator)

    return asyncio.run(run())


def test_the_cassettes_replay_the_run_they_recorded() -> None:
    """No simulator behind this one — only the JSONL in fixtures/."""
    import asyncio

    async def run() -> list[Phase]:
        async with replay_run(cassette=True) as orchestrator:
            assert orchestrator.settings.replay_cassette
            return await run_timeline(orchestrator)

    phases = asyncio.run(run())
    assert [phase.as_dict() for phase in phases] == TIMELINE


def test_the_simulated_fork_still_agrees_with_the_cassettes(simulated: list[Phase]) -> None:
    assert [phase.as_dict() for phase in simulated] == TIMELINE


def test_replay_boots_with_no_credentials_and_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole constraint in one test: an evaluator has no `.env`."""
    from orchestrator import transport as transport_module
    from orchestrator.config import Settings, get_settings

    for name in ("GITHUB_TOKEN", "DEVIN_API_KEY", "DEVIN_ORG_ID", "GITHUB_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPLAY", "true")
    get_settings.cache_clear()
    settings = Settings()
    assert settings.github_token and settings.devin_api_key and settings.devin_org_id
    # An empty secret would make the receiver accept anything, so `make simulate`
    # would prove nothing about the HMAC path.
    assert settings.github_webhook_secret
    assert settings.batch_window_seconds < 60  # the live cadence, compressed

    for service in ("github", "devin"):
        built = transport_module.build_transport(service)
        assert built is not None
        assert not isinstance(built, transport_module.RecordingTransport)
    get_settings.cache_clear()


def test_every_branch_of_the_state_machine_is_exercised(simulated: list[Phase]) -> None:
    seen = {state for phase in simulated for state in phase.states.values()}
    assert {
        State.DEVIN_ELIGIBLE.value,
        State.DEVIN_WORKING.value,
        State.DEVIN_PR_OPEN.value,
        State.DEVIN_FIXING.value,
        State.HUMAN_REVIEW.value,
        State.DEVIN_DECLINED.value,
        State.DONE.value,
    } <= seen


def test_the_board_settles_where_the_answer_key_says(simulated: list[Phase]) -> None:
    final = simulated[-1].states
    # 1 and 7 merge clean, 3 merges after an autofix round, 6 stays red until the
    # round cap escalates it, 8 is below the confidence threshold, 18 is declined.
    assert final == {
        1: State.DONE.value,
        3: State.DONE.value,
        6: State.HUMAN_REVIEW.value,
        7: State.DONE.value,
        8: State.HUMAN_REVIEW.value,
        18: State.DEVIN_DECLINED.value,
    }


def test_acu_accumulates_and_the_ci_loop_is_not_free(simulated: list[Phase]) -> None:
    burn = [phase.acus for phase in simulated]
    assert burn == sorted(burn)
    assert burn[-1] > 0
    # The fixed issue cost strictly more than the clean one of the same tier.
    assert simulated[-1].acus > simulated[4].acus


def test_metrics_are_derived_from_the_replayed_board() -> None:
    import asyncio

    async def run() -> dict[str, object]:
        async with replay_run(cassette=True) as orchestrator:
            await run_timeline(orchestrator)
            return compute(
                orchestrator.store.cards(),
                orchestrator.store.sessions(),
                orchestrator.metrics.escalations,
            )

    metrics = asyncio.run(run())
    headline = metrics["headline"]  # type: ignore[index]
    funnel = metrics["funnel"]  # type: ignore[index]
    assert headline["merged"] == 3
    assert headline["total_acu"] > 0
    assert headline["acu_per_merged_pr"]
    assert funnel["ingested"] == 6
    assert funnel["pr_opened"] == 4
    assert "ci-unfixable" in metrics["escalations"]  # type: ignore[index]
    assert "low-confidence" in metrics["escalations"]  # type: ignore[index]


def test_a_broken_transition_fails_the_suite() -> None:
    """The point of the phase table: it has to be able to go red."""
    broken = copy.deepcopy(TIMELINE)
    broken[-1]["states"]["6"] = State.DONE.value
    assert broken != TIMELINE


def test_a_cassette_miss_is_loud_in_strict_mode() -> None:
    transport = ReplayTransport(FIXTURES / "devin.jsonl", strict=True)
    assert not transport.empty
    import httpx

    request = httpx.Request("GET", "https://api.devin.ai/v3/organizations/nope/sessions/nope")
    with pytest.raises(CassetteMissError):
        import asyncio

        asyncio.run(transport.handle_async_request(request))


def test_recorded_deliveries_are_signable_and_filtered() -> None:
    deliveries = load_deliveries()
    secret = "replay-secret"
    for delivery in deliveries:
        body = json.dumps(delivery["payload"]).encode()
        assert verify_signature(secret, body, sign(secret, body))
        assert not verify_signature(secret, body + b" ", sign(secret, body))
    # Devin's own relabelling and CI's own check_run: neither is a human asking
    # for triage, and re-triaging on them is how a webhook loop starts.
    bots = [d for d in deliveries if is_bot_sender(d["payload"], ["devin-ai-integration"])]
    assert {d["event"] for d in bots} == {"issues", "check_run"}
    assert {d["event"] for d in deliveries} == {"issues", "issue_comment", "check_run"}
    assert len({d["delivery"] for d in deliveries}) == len(deliveries)
