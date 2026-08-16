"""What a sweep is allowed to spend.

The orchestrator polls a fork it does not own, out of an hourly budget it
shares with everything else using the same credential. Correctness is settled
elsewhere; these tests are about the sweep asking for as little as it can and
still being right — a projection that is cheap to keep is a projection that can
be kept every 15 seconds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.dispatch import Dispatcher
from orchestrator.labels import State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import CheckRun, Issue, IssueMeta, SessionInfo
from orchestrator.poller import Poller
from orchestrator.state import Store

NOW = 1_800_000_000.0


def issue(number: int, labels: list[str] | None = None, state: str = "open") -> Issue:
    return Issue.model_validate(
        {
            "number": number,
            "title": f"issue {number}",
            "labels": labels or [],
            "state": state,
        }
    )


def pull_request(number: int, issue_number: int, merged: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "body": f"Closes #{issue_number}",
        "state": "closed" if merged else "open",
        "merged_at": "2026-08-16T00:00:00Z" if merged else None,
        "head": {"ref": f"devin/issue-{issue_number}-fix", "sha": f"sha{number}"},
    }


class RecordingGitHub:
    """Answers a sweep, and remembers what it was asked for."""

    def __init__(self, issues: list[Issue], prs: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues
        self.prs = prs or []
        self.now = NOW
        self.since_asked: list[str | None] = []
        self.check_refs: list[str] = []
        self.states: list[State] = []

    def server_time(self) -> float:
        return self.now

    @property
    def budget(self) -> dict[str, Any]:
        return {"remaining": 4900, "limit": 5000, "reserve": 100, "resets_at": self.now + 3600}

    async def list_issues(
        self, state: str = "all", limit: int = 100, since: str | None = None
    ) -> list[Issue]:
        self.since_asked.append(since)
        return self.issues

    async def list_open_prs(self) -> list[dict[str, Any]]:
        return self.prs

    async def check_runs_for_ref(self, ref: str) -> list[CheckRun]:
        self.check_refs.append(ref)
        return [CheckRun(name="pre-commit", status="in_progress")]

    async def find_meta_comment(self, number: int) -> None:
        return None

    async def set_state(self, number: int, target: State, current_labels: list[str]) -> None:
        self.states.append(target)

    async def add_labels(self, number: int, labels: list[str]) -> None:
        pass

    async def upsert_meta(self, number: int, meta: Any, note: str = "") -> None:
        pass


class SilentDevin:
    async def list_by_tags(self, tags: list[str]) -> list[SessionInfo]:
        return []


def poller_for(github: RecordingGitHub, settings: Settings | None = None) -> Poller:
    settings = settings or Settings(replay=True)
    store = Store()
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.settings = settings
    dispatcher.store = store
    dispatcher.github = github  # type: ignore[assignment]
    dispatcher.metrics = MetricsRegistry()

    poller = Poller.__new__(Poller)
    poller.settings = settings
    poller.github = github  # type: ignore[assignment]
    poller.devin = SilentDevin()  # type: ignore[assignment]
    poller.store = store
    poller.dispatcher = dispatcher
    poller.metrics = MetricsRegistry()
    poller._consumed = set()
    poller._swept_through = None
    poller._last_full_sweep = 0.0
    return poller


# --- incremental sweeps --------------------------------------------------------


async def test_the_steady_state_sweep_only_asks_for_what_moved() -> None:
    github = RecordingGitHub([issue(7)])
    poller = poller_for(github)

    await poller.reconcile()  # first sweep has nothing to be incremental from
    github.now += 180
    await poller.reconcile()

    expected = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert github.since_asked[0] is None
    assert github.since_asked[1] == expected


async def test_the_since_boundary_overlaps_rather_than_trusting_the_clock() -> None:
    """An issue touched while the sweep was in flight must land in the next
    window, so `since` is rewound rather than set to when the sweep ran."""
    settings = Settings(replay=True, reconcile_overlap_seconds=60)
    github = RecordingGitHub([issue(7)])
    poller = poller_for(github, settings)

    await poller.reconcile()

    assert poller._swept_through == github.now - 60


async def test_a_full_sweep_still_happens_on_schedule() -> None:
    """`since` cannot report a deletion, or an issue the projection never had."""
    settings = Settings(replay=True, full_reconcile_seconds=3600)
    github = RecordingGitHub([issue(7)])
    poller = poller_for(github, settings)

    await poller.reconcile()
    github.now += 1800
    await poller.reconcile()
    assert github.since_asked[-1] is not None  # not due yet

    github.now += 1801
    await poller.reconcile()
    assert github.since_asked[-1] is None


async def test_an_issue_that_is_gone_leaves_the_board_on_the_full_sweep() -> None:
    github = RecordingGitHub([issue(7), issue(8)])
    poller = poller_for(github, Settings(replay=True, full_reconcile_seconds=3600))
    await poller.reconcile()
    assert len(poller.store.cards()) == 2

    github.issues = [issue(7)]
    github.now += 3601
    await poller.reconcile()

    assert [card.number for card in poller.store.cards()] == [7]


# --- what is worth re-reading --------------------------------------------------


async def test_checks_are_read_for_a_pr_that_is_still_waiting_on_ci() -> None:
    github = RecordingGitHub([issue(7, ["devin-pr-open"])], [pull_request(101, 7)])
    poller = poller_for(github)

    await poller.reconcile()

    assert github.check_refs == ["sha101"]
    card = poller.store.card(7)
    assert card is not None and card.pr_number == 101


async def test_a_merged_pr_is_not_re_read_but_still_lands_the_card() -> None:
    """The checks that mattered are already in the store; the merge is the
    answer, and reading them again cannot change it."""
    github = RecordingGitHub([issue(7, ["devin-pr-open"])], [pull_request(101, 7, merged=True)])
    poller = poller_for(github)

    await poller.reconcile()

    assert github.check_refs == []
    card = poller.store.card(7)
    assert card is not None and card.state is State.DONE
    assert github.states == [State.DONE]


async def test_a_terminal_card_stops_costing_a_check_read_every_sweep() -> None:
    github = RecordingGitHub([issue(7, ["human-review"])], [pull_request(101, 7)])
    poller = poller_for(github)

    await poller.reconcile()
    github.now += 180
    await poller.reconcile()

    assert github.check_refs == []


async def test_the_remaining_budget_reaches_the_dashboard() -> None:
    github = RecordingGitHub([issue(7)])
    poller = poller_for(github)

    await poller.reconcile()

    assert poller.store.snapshot()["budget"]["remaining"] == 4900


# --- booting into a bad network ------------------------------------------------


class FlakyGitHub(RecordingGitHub):
    """Fails the first sweep the way a dropped keep-alive socket does."""

    def __init__(self, issues: list[Issue], failures: int) -> None:
        super().__init__(issues)
        self.failures = failures

    async def list_issues(
        self, state: str = "all", limit: int = 100, since: str | None = None
    ) -> list[Issue]:
        if self.failures > 0:
            self.failures -= 1
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return await super().list_issues(state, limit, since)


async def test_a_dropped_connection_on_the_first_sweep_does_not_kill_the_process() -> None:
    """The projection is derived from GitHub, so a first sync that fails costs
    latency, not correctness — exiting turns a lost socket into an outage."""
    github = FlakyGitHub([issue(7)], failures=1)
    poller = poller_for(github, Settings(replay=True, poll_waiting_seconds=0))

    await poller.start()
    try:
        assert not poller.store.first_sync.is_set()
        await asyncio.wait_for(poller.store.first_sync.wait(), timeout=2)
        assert poller.store.card(7) is not None
    finally:
        await poller.stop()


async def test_a_card_that_has_not_noticed_its_pr_is_still_promoted() -> None:
    """The saving must not be taken on the transition that needs the read: a
    worker's PR appears while the card still says `devin-eligible`, and nothing
    but the check sweep moves it on."""
    github = RecordingGitHub([issue(24, ["devin-eligible", "tier:medium"])], [pull_request(32, 24)])
    poller = poller_for(github)

    await poller.reconcile()

    assert github.check_refs == ["sha32"]
    card = poller.store.card(24)
    assert card is not None and card.state is State.DEVIN_PR_OPEN


async def test_a_zero_worker_cap_also_stops_ci_autofix() -> None:
    """`MAX_CONCURRENT_WORKERS=0` has to mean "spend nothing", not "spend only
    on CI"."""
    github = RecordingGitHub([issue(25, ["devin-pr-open", "tier:medium"])], [pull_request(35, 25)])
    poller = poller_for(github, Settings(replay=True, max_concurrent_workers=0))
    card = poller.store.upsert_issue(issue(25, ["devin-pr-open", "tier:medium"]), IssueMeta())

    started = await poller.dispatcher.dispatch_ci_fix(
        card, [CheckRun(name="jest", status="completed", conclusion="failure")]
    )

    assert started is False
