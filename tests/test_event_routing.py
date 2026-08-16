"""What the sender filter decides, at the level it actually matters: which
events start a triage batch and which ones count as a human turn.

The switch from a PAT to a GitHub App changes the orchestrator's own login from
a human's to `<slug>[bot]`, so these are the assertions that catch the filter
over- or under-reaching after that change.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings
from orchestrator.labels import State
from orchestrator.service import Orchestrator
from orchestrator.webhooks import Debouncer

APP_LOGIN = "cgsol-orchestrator[bot]"


class Harness:
    """Just enough Orchestrator to route an event. No GitHub, no Devin."""

    def __init__(self, app_slug: str = "") -> None:
        self.queued: list[int] = []
        self.human_turns: list[int] = []
        service = Orchestrator.__new__(Orchestrator)
        service.settings = Settings(replay=True, github_app_slug=app_slug)
        service.debouncer = Debouncer(60, self._flush)
        service._count_human_turn = self.human_turns.append  # type: ignore[method-assign]
        service._refresh_issue = self._refresh  # type: ignore[method-assign]
        self.service = service

    async def _flush(self, batch: set[int]) -> None:  # pragma: no cover - never fires here
        self.queued.extend(sorted(batch))

    async def _refresh(self, number: int) -> None:
        return None

    async def deliver(self, sender: str, **overrides: Any) -> str:
        payload: dict[str, Any] = {
            "action": "labeled",
            "issue": {"number": 7, "labels": [{"name": State.NEEDS_TRIAGE.value}]},
            "sender": {"login": sender},
        }
        payload.update(overrides)
        return await self.service.handle_event("issues", payload)

    @property
    def pending(self) -> set[int]:
        return self.service.debouncer.pending


async def test_our_own_seeding_still_starts_triage_as_an_app() -> None:
    """`make seed` labels the backlog `needs-triage` under the orchestrator's own
    identity. As a PAT that read as a human and queued; as an App it reads as a
    bot, and a blanket bot filter would silently swallow the event that starts
    the entire pipeline."""
    harness = Harness(app_slug="cgsol-orchestrator")
    assert await harness.deliver(APP_LOGIN) == "queued"
    assert harness.pending == {7}


async def test_devins_label_writes_stay_inert() -> None:
    harness = Harness(app_slug="cgsol-orchestrator")
    assert await harness.deliver("devin-ai-integration[bot]") == "refreshed"
    assert harness.pending == set()


async def test_a_human_still_queues() -> None:
    harness = Harness(app_slug="cgsol-orchestrator")
    assert await harness.deliver("dpeachpeach") == "queued"
    assert harness.pending == {7}


async def test_our_own_writes_are_not_human_turns() -> None:
    """The autonomy metric is "merged with zero human turns". Under the PAT the
    orchestrator's own label writes were counted as human ones, which understated
    it; as an App they are correctly attributed."""
    harness = Harness(app_slug="cgsol-orchestrator")
    labelled = {"issue": {"number": 7, "labels": [{"name": State.DEVIN_WORKING.value}]}}

    await harness.deliver(APP_LOGIN, **labelled)
    await harness.deliver("devin-ai-integration[bot]", **labelled)
    assert harness.human_turns == []

    await harness.deliver("dpeachpeach", **labelled)
    assert harness.human_turns == [7]


async def test_pat_mode_routes_exactly_as_before() -> None:
    """No app configured: there is no self identity to recognise, and the human
    login on the PAT behaves the way it did before any of this."""
    harness = Harness(app_slug="")
    assert await harness.deliver("dpeachpeach") == "queued"
    assert await harness.deliver("devin-ai-integration[bot]") == "refreshed"
