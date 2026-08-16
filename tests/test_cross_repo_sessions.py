"""Two deployments in one Devin org must not adopt each other's sessions.

Sessions are org-scoped and issue numbers collide freely — every deployment
seeds the same corpus into a fresh repository, so both boards have an issue #37.
Matching a session to a card on its `issue:` tag alone therefore attaches work
from repo A to a card in repo B, and the resulting board looks entirely
plausible while citing pull requests from somewhere else.
"""

from __future__ import annotations

import asyncio

from orchestrator.config import Settings
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import Issue, SessionInfo
from orchestrator.poller import Poller
from orchestrator.state import Store


def issue(number: int) -> Issue:
    return Issue.model_validate({"number": number, "title": f"#{number}", "state": "open"})


def session(session_id: str, tags: list[str]) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        tags=tags,
        status="running",
        status_enum="working",
        pr_url="https://github.com/owner/other/pull/58",
        acus_consumed=2.0,
    )


def store_for(repo: str) -> Store:
    store = Store(repo=repo)
    store.upsert_issue(issue(37))
    return store


def test_a_session_from_another_repo_never_reaches_a_card() -> None:
    store = store_for("dpeachpeach-superset-cg2")

    store.upsert_session(
        session("devin-1", ["cgsol", "role:worker", "repo:dpeachpeach-superset-cg", "issue:37"])
    )

    card = store.card(37)
    assert card is not None
    assert card.session is None
    assert card.meta.pr_url is None
    assert card.meta.acus == 0.0
    assert store.sessions() == []
    assert store.active_worker_count() == 0


def test_a_session_from_this_repo_still_does() -> None:
    store = store_for("dpeachpeach-superset-cg2")

    store.upsert_session(
        session("devin-1", ["cgsol", "role:worker", "repo:dpeachpeach-superset-cg2", "issue:37"])
    )

    card = store.card(37)
    assert card is not None
    assert card.session is not None
    assert card.meta.acus == 2.0


def test_a_session_with_no_repo_tag_is_still_ours() -> None:
    """It predates the tag. Rejecting it would orphan work that keeps billing
    while the board quietly stops tracking it."""
    store = store_for("dpeachpeach-superset-cg2")

    store.upsert_session(session("devin-1", ["cgsol", "role:worker", "issue:37"]))

    card = store.card(37)
    assert card is not None
    assert card.session is not None


class ForeignDevin:
    """Answers the tag query as an unnarrowed Devin would: with everything."""

    def __init__(self, detail: SessionInfo) -> None:
        self.detail = detail
        self.queries: list[list[str]] = []
        self.terminated: list[str] = []
        self.gets = 0

    async def list_by_tags(self, tags: list[str]) -> list[SessionInfo]:
        self.queries.append(tags)
        # The list payload omits the repo tag, so the summary looks like ours.
        return [self.detail.model_copy(update={"tags": ["cgsol", "role:scout", "issue:37"]})]

    async def get_session(self, session_id: str) -> SessionInfo:
        self.gets += 1
        return self.detail

    async def terminate(self, session_id: str) -> bool:
        self.terminated.append(session_id)
        return True


async def test_the_poller_asks_for_its_own_repo_and_drops_what_isnt() -> None:
    settings = Settings(github_repo="dpeachpeach/superset-cg2", replay=True)
    detail = session(
        "devin-1",
        ["cgsol", "role:scout", "repo:dpeachpeach-superset-cg", "issue:37"],
    ).model_copy(update={"status": "exit"})
    devin = ForeignDevin(detail)

    poller = Poller.__new__(Poller)
    poller.settings = settings
    poller.devin = devin  # type: ignore[assignment]
    poller.store = store_for(settings.repo_slug)
    poller.metrics = MetricsRegistry()
    poller._consumed = set()
    poller._foreign = set()
    poller._swept_through = None
    poller._last_full_sweep = 0.0
    poller._sweeping = asyncio.Lock()

    await poller.poll_sessions()

    assert devin.queries == [["cgsol", "repo:dpeachpeach-superset-cg2"]]
    assert poller.store.sessions() == []
    # Consuming a foreign session ends in terminating one another deployment is
    # still running, which is worse than ignoring it.
    assert devin.terminated == []

    await poller.poll_sessions()  # and it is remembered, not re-read
    assert devin.gets == 1


def test_the_tag_the_dispatcher_writes_is_the_tag_the_store_reads() -> None:
    """The two halves are written in different modules; a rename in one that
    misses the other silently disables the check rather than breaking a test."""
    settings = Settings(github_repo="dpeachpeach/superset-cg2", replay=True)
    tagged = session("devin-1", ["cgsol", settings.repo_tag, "issue:37"])

    assert tagged.repo == settings.repo_slug
    assert Store(repo=settings.repo_slug).owns(tagged)
