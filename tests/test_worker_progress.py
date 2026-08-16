"""Workers announcing what they are doing, and the orchestrator believing them
only as far as it should.

The value of the channel is entirely in its cost: a progress comment is written
on Devin's quota and delivered as a webhook, so the board can show work in
flight for zero reads against ours. That property is fragile — one reflexive
`_refresh_issue` in this path and the cheapest signal we have becomes the most
expensive — so the no-refetch assertion below matters more than the rest.

What it is not is a source of truth. A worker can say it is drafting; it cannot
say the PR is open or CI is green. Those still come from GitHub.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings
from orchestrator.labels import State
from orchestrator.models import Issue
from orchestrator.service import Orchestrator
from orchestrator.state import Store
from orchestrator.webhooks import parse_progress

DEVIN = "devin-ai-integration[bot]"
APP_LOGIN = "cgsol-orchestrator[bot]"


class Harness:
    """An Orchestrator with GitHub amputated: any read raises, so a test that
    ends green has proved the handler never asked GitHub anything."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[str] = []
        service = Orchestrator.__new__(Orchestrator)
        service.settings = Settings(replay=True, github_app_slug="cgsol-orchestrator")
        service.store = Store()
        service.store.upsert_issue(
            Issue.model_validate(
                {
                    "number": 24,
                    "title": "bump a pinned dependency",
                    "labels": [State.DEVIN_WORKING.value],
                    "state": "open",
                }
            )
        )
        service.store.publish = self._publish  # type: ignore[method-assign]
        service._refresh_issue = self._forbidden("refresh_issue")  # type: ignore[method-assign]
        service._count_human_turn = lambda number: self.reads.append("human_turn")  # type: ignore[method-assign,misc]
        self.service = service

    def _forbidden(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> None:
            raise AssertionError(f"progress handling must not call {name}")

        return call

    async def _publish(self, event: str, data: dict[str, Any]) -> None:
        self.published.append((event, data))

    async def comment(
        self,
        body: str,
        sender: str = DEVIN,
        comment_id: int = 900,
        action: str = "created",
    ) -> str:
        return await self.service.handle_event(
            "issue_comment",
            {
                "action": action,
                "issue": {"number": 24},
                "comment": {
                    "id": comment_id,
                    "body": body,
                    "created_at": "2026-08-16T03:15:00Z",
                },
                "sender": {"login": sender},
            },
        )

    @property
    def card(self) -> Any:
        return self.service.store.card(24)


DRAFTING = "CGSOL_PROGRESS: drafting-pr Preparing the smallest viable fix."


# --- the contract ------------------------------------------------------------


def test_the_prefix_carries_phase_and_prose() -> None:
    progress = parse_progress(
        {
            "action": "created",
            "issue": {"number": 24},
            "comment": {"id": 5, "body": DRAFTING, "created_at": "2026-08-16T03:15:00Z"},
        }
    )
    assert progress is not None
    assert (progress.issue, progress.phase, progress.comment_id) == (24, "drafting-pr", 5)
    assert progress.message == "Preparing the smallest viable fix."


def test_a_bare_phase_needs_no_prose() -> None:
    progress = parse_progress(
        {
            "action": "created",
            "issue": {"number": 24},
            "comment": {"id": 5, "body": "CGSOL_PROGRESS: drafting-pr"},
        }
    )
    assert progress is not None
    assert progress.message == ""


def test_an_invented_phase_is_not_a_board_state() -> None:
    """The phase is rendered on a card, so the set of phases is closed. A worker
    that writes `CGSOL_PROGRESS: merged` is making a claim it has no standing to
    make, and the parser is where that stops."""
    assert (
        parse_progress(
            {
                "action": "created",
                "issue": {"number": 24},
                "comment": {"id": 5, "body": "CGSOL_PROGRESS: merged"},
            }
        )
        is None
    )


def test_ordinary_prose_is_not_progress() -> None:
    assert (
        parse_progress(
            {
                "action": "created",
                "issue": {"number": 24},
                "comment": {"id": 5, "body": "I am drafting a PR now"},
            }
        )
        is None
    )


def test_an_edited_comment_is_not_a_new_event() -> None:
    assert (
        parse_progress(
            {
                "action": "edited",
                "issue": {"number": 24},
                "comment": {"id": 5, "body": DRAFTING},
            }
        )
        is None
    )


# --- routing ------------------------------------------------------------------


async def test_the_card_updates_from_the_payload_alone() -> None:
    harness = Harness()
    assert await harness.comment(DRAFTING) == "progress"

    card = harness.card
    assert card.progress_phase == "drafting-pr"
    assert card.progress_message == "Preparing the smallest viable fix."
    assert card.progress_at == "2026-08-16T03:15:00Z"
    assert card.progress_comment_id == 900
    # The label is still the state. Progress is narration over the top of it.
    assert card.state is State.DEVIN_WORKING


async def test_the_frontend_hears_about_it() -> None:
    harness = Harness()
    await harness.comment(DRAFTING)
    assert harness.published == [
        (
            "worker.progress",
            {
                "issue": 24,
                "phase": "drafting-pr",
                "message": "Preparing the smallest viable fix.",
                "at": "2026-08-16T03:15:00Z",
            },
        )
    ]


async def test_a_redelivery_changes_nothing() -> None:
    """Smee and GitHub both retry. Twice-delivered must mean once-applied."""
    harness = Harness()
    assert await harness.comment(DRAFTING) == "progress"
    assert await harness.comment(DRAFTING) == "ignored"
    assert len(harness.published) == 1


async def test_a_later_comment_supersedes_the_first() -> None:
    harness = Harness()
    await harness.comment(DRAFTING)
    assert await harness.comment("CGSOL_PROGRESS: pr-opened", comment_id=901) == "progress"
    assert harness.card.progress_phase == "pr-opened"


async def test_our_own_comments_are_never_progress() -> None:
    """The orchestrator writes the metadata comment on every issue. If its own
    writes could parse as progress, the projection would be reacting to itself."""
    harness = Harness()
    assert await harness.comment(DRAFTING, sender=APP_LOGIN) == "ignored"
    assert harness.card.progress_phase is None


async def test_a_human_saying_the_magic_words_is_still_a_human_turn() -> None:
    """A maintainer pasting the prefix is not a worker. It counts as human
    engagement, which is what the autonomy figure is measuring."""
    harness = Harness()
    assert await harness.comment(DRAFTING, sender="dpeachpeach") == "noted"
    assert harness.card.progress_phase is None
    assert harness.reads == ["human_turn"]


async def test_devins_other_comments_stay_inert() -> None:
    harness = Harness()
    assert await harness.comment("Ready to merge — confidence 0.85.") == "ignored"
    assert harness.card.progress_phase is None
