"""Polling and reconciliation.

Webhooks give latency; polling gives correctness. Both run: an event moves an
issue within a second, and a full sweep every few minutes repairs whatever the
event stream dropped, duplicated, or delivered while the process was restarting.
That is the intended pattern, not a fallback hack — a webhook is a hint that
state changed, never the record of what it changed to.

Polling is one tag-filtered list call per cycle, not one GET per session, and
sessions whose result has been consumed are never fetched again.

A session's result is consumed when it *settles*, which is not the same as
finishing: Devin idles at ``waiting_for_user`` when it has said its piece and
nobody replies, and only reaches ``finished`` if something puts it to sleep. A
scout that produced its verdicts and then waited would otherwise sit there
holding them forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from orchestrator.config import Settings
from orchestrator.devin import DevinClient
from orchestrator.dispatch import Dispatcher
from orchestrator.github import GitHubClient, RateLimited
from orchestrator.labels import TERMINAL_STATES, State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import IssueCard, IssueMeta, SessionInfo, Verdict
from orchestrator.state import Store

log = logging.getLogger("cgsol.poller")

#: The states whose next move is CI's to make. Anywhere else the last known
#: checks are already in the store and re-reading them cannot change an answer,
#: and check-runs is the most expensive read in the loop: one request per PR per
#: sweep, uncacheable for as long as CI is churning.
CI_WATCH_STATES: frozenset[State] = frozenset(
    {State.DEVIN_PR_OPEN, State.CI_FAILING, State.DEVIN_FIXING}
)


class Poller:
    def __init__(
        self,
        settings: Settings,
        github: GitHubClient,
        devin: DevinClient,
        store: Store,
        dispatcher: Dispatcher,
        metrics: MetricsRegistry,
    ) -> None:
        self.settings = settings
        self.github = github
        self.devin = devin
        self.store = store
        self.dispatcher = dispatcher
        self.metrics = metrics
        self._consumed: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        #: On GitHub's clock, from the sweep that read it.
        self._swept_through: float | None = None
        self._last_full_sweep: float = 0.0

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        try:
            await self.reconcile()
            self.store.first_sync.set()
        except RateLimited as limit:
            # Refusing to boot until GitHub answers turns an hour of thin
            # budget into an hour of downtime. Come up unhealthy and converge.
            log.warning("first sync deferred: %s", limit)
            self._tasks = [asyncio.create_task(self._recover(limit), name="cgsol-recover")]
            return
        self._tasks = [
            asyncio.create_task(self._session_loop(), name="cgsol-sessions"),
            asyncio.create_task(self._reconcile_loop(), name="cgsol-reconcile"),
        ]

    async def _recover(self, limit: RateLimited) -> None:
        """Retry the first sync until it lands, then run the loops as normal."""
        while not self.store.first_sync.is_set():
            await self._wait_out(limit)
            try:
                await self.reconcile()
                self.store.first_sync.set()
            except RateLimited as again:
                limit = again
            except Exception:
                log.exception("deferred first sync failed")
        self._tasks += [
            asyncio.create_task(self._session_loop(), name="cgsol-sessions"),
            asyncio.create_task(self._reconcile_loop(), name="cgsol-reconcile"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _session_loop(self) -> None:
        while True:
            try:
                await self.poll_sessions()
                await self.dispatcher.dispatch_ready()
                self.metrics.sample(self.store.cards(), self.store.sessions())
                self.store.budget = self.github.budget
                await self.store.publish("tick", {"synced_at": time.time()})
            except asyncio.CancelledError:
                raise
            except RateLimited as limit:
                await self._wait_out(limit)
                continue
            except Exception:  # keep the loop alive; a bad cycle is not fatal
                log.exception("session poll failed")
            await asyncio.sleep(self._interval())

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.reconcile_seconds)
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except RateLimited as limit:
                await self._wait_out(limit)
            except Exception:
                log.exception("reconcile failed")

    async def _wait_out(self, limit: RateLimited) -> None:
        """Sleep off an exhausted budget rather than spending the next hour
        collecting 403s. A projection that stops updating for a while is
        recoverable; one built from failed reads is not."""
        delay = max(30.0, limit.resets_at - time.time() + 5)
        log.warning("github rate limited; pausing polling for %.0fs", delay)
        await self.store.publish("rate_limited", {"resets_at": limit.resets_at})
        await asyncio.sleep(delay)

    def _interval(self) -> float:
        sessions = [s for s in self.store.sessions() if not s.terminal]
        if not sessions:
            return self.settings.poll_waiting_seconds
        if any(not session.waiting for session in sessions):
            return self.settings.poll_active_seconds
        return self.settings.poll_waiting_seconds

    # --- sessions -------------------------------------------------------------

    async def poll_sessions(self) -> None:
        if not self.settings.devin_api_key and not self.settings.replay:
            return
        sessions = await self.devin.list_by_tags([self.settings.tag_namespace])
        for session in sessions:
            previous = self.store.session(session.session_id)
            self.store.upsert_session(session)
            if previous is None and session.origin == "automation":
                # Discovered, not dispatched: an Automation created this session
                # and nobody told us. Convergence, not notification. Adoption is
                # independent of status, because first sight and stopped are not
                # mutually exclusive and there is no second first sight.
                await self._adopt(session)
            settled = session.terminal or session.waiting
            # One GET, and only for a session that has stopped: a waiting session
            # may be holding a result or may just be blocked, and the list payload
            # does not carry structured output either way.
            if (
                settled
                and session.session_id not in self._consumed
                and await self._on_settled(session)
            ):
                self._consumed.add(session.session_id)
                await self._retire(session)

    async def _retire(self, session: SessionInfo) -> None:
        """Close a session we are finished reading. Best-effort: the result is
        already banked, so a failure here costs a held slot, not correctness."""
        if session.terminal or not self.settings.terminate_consumed_sessions:
            return
        try:
            if await self.devin.terminate(session.session_id):
                self.store.upsert_session(session.model_copy(update={"status": "exit"}))
        except Exception:
            log.warning("could not terminate %s", session.session_id, exc_info=True)

    async def _adopt(self, session: SessionInfo) -> None:
        number = session.issue_number
        if number is None:
            return
        card = self.store.card(number)
        if card is None:
            return
        card.meta.session_id = session.session_id
        await self.store.publish(
            "session.adopted",
            {"issue": number, "session_id": session.session_id, "role": session.role},
        )

    async def _on_settled(self, summary: SessionInfo) -> bool:
        """Consume a stopped session's result. False means try again next cycle.

        A session that stopped without a result is not necessarily done with:
        ``waiting_for_user`` is also how Devin asks a question mid-flight, and
        answering it later produces the output. Only a terminal session's
        emptiness is final.
        """
        detail = await self.devin.get_session(summary.session_id)
        self.store.upsert_session(detail)
        output: dict[str, Any] | None = detail.structured_output
        role = detail.role or summary.role

        if role == "scout":
            verdicts = _parse_verdicts(output)
            if not verdicts:
                if detail.terminal:
                    log.warning("scout %s produced no verdicts", detail.session_id)
                    return True
                return False
            # Structured output is readable while the session is still writing
            # it, so a batch can be seen half-finished. Apply what is there —
            # a verdict is worth having early, and applying is idempotent — but
            # only stop polling once every issue in the batch is accounted for,
            # or the session ends and the rest is never coming.
            await self.dispatcher.apply_verdicts(verdicts)
            expected = set(detail.issue_numbers)
            missing = expected - {verdict.issue_number for verdict in verdicts}
            if missing and not detail.terminal:
                log.info(
                    "scout %s has %d/%d verdicts; waiting for %s",
                    detail.session_id,
                    len(expected) - len(missing),
                    len(expected),
                    sorted(missing),
                )
                return False
            await self.store.publish("scout.finished", {"verdicts": len(verdicts)})
            return True

        number = detail.issue_number
        card = self.store.card(number) if number is not None else None
        if card is None:
            return True
        card.meta.acus = max(card.meta.acus, detail.acus_consumed)
        if detail.status == "error":
            await self.dispatcher.escalate(card, "session-error", State.DEVIN_BLOCKED)
            return True
        if output is None and not detail.terminal:
            return False
        if role == "ci-fix":
            await self.dispatcher.on_ci_fix_finished(card, output)
        elif role == "worker":
            await self.dispatcher.on_worker_finished(card, output)
        return True

    # --- reconcile ------------------------------------------------------------

    async def reconcile(self, full: bool | None = None) -> None:
        """Make the projection agree with GitHub, reading as little as possible.

        The steady-state sweep asks only for issues touched since the last one.
        A periodic full sweep still runs, because `since` cannot report what is
        no longer there, and an issue the projection never saw is not 'touched'.
        """
        full = self._full_sweep_due() if full is None else full
        # Read the clock before the request, not after: anything that changes
        # while it is in flight has to fall inside the next window.
        swept_at = self.github.server_time()
        since = None if full else _iso8601(self._swept_through)
        issues = await self.github.list_issues(state="all", limit=100, since=since)
        prs = await self.github.list_open_prs()
        pr_by_issue = _index_prs(prs)

        for issue in issues:
            meta = None
            existing = self.store.card(issue.number)
            if existing is None or not existing.meta.session_id:
                found = await self.github.find_meta_comment(issue.number)
                meta = found[1] if found else IssueMeta()
            card = self.store.upsert_issue(issue, meta)
            # A close is only this pipeline's outcome if this pipeline was
            # working the issue. Closing something it never touched is a
            # maintainer clearing their own backlog, and closing something
            # already terminal does not rewrite how it got there — count either
            # as done and every headline that divides by merges inflates.
            if (
                issue.state == "closed"
                and card.state is not None
                and card.state not in TERMINAL_STATES
            ):
                card.state = State.DONE

        # PRs are one list call regardless, and CI moving does not touch the
        # issue it belongs to, so this runs over every linked PR rather than
        # only the issues this sweep happened to list.
        for number, pr in pr_by_issue.items():
            await self._reconcile_pr(number, pr)

        self._swept_through = swept_at - self.settings.reconcile_overlap_seconds
        if full:
            self._last_full_sweep = swept_at
            # Only when the listing was not truncated: past the page cap the
            # issues that did not come back are not gone, just unread.
            if len(issues) < 100:
                for number in self.store.retain({issue.number for issue in issues}):
                    log.info("dropping card #%s: no longer on the fork", number)

        await self.poll_sessions()
        self.metrics.sample(self.store.cards(), self.store.sessions())
        self.store.budget = self.github.budget
        await self.store.publish("reconciled", self.store.snapshot())

    def _full_sweep_due(self) -> bool:
        if self._swept_through is None:
            return True
        return (
            self.github.server_time() - self._last_full_sweep
            >= self.settings.full_reconcile_seconds
        )

    async def _reconcile_pr(self, number: int, pr: dict[str, Any]) -> None:
        card = self.store.card(number)
        if card is None:
            return
        card.pr_number = pr["number"]
        card.meta.pr_url = pr["html_url"]
        card.pr_merged = bool(pr.get("merged_at"))
        if not _checks_can_still_move(card, pr):
            # A merged PR still has to land the card on `done`; it just does not
            # need its checks re-read to say so.
            if card.pr_merged:
                await self.dispatcher.evaluate_ci(card, card.checks, True)
            return
        checks = await self.github.check_runs_for_ref(pr["head"]["sha"])
        self.store.set_checks(number, checks)
        await self.dispatcher.evaluate_ci(card, checks, card.pr_merged)


def _checks_can_still_move(card: IssueCard, pr: dict[str, Any]) -> bool:
    """Whether reading this PR's check runs can still change anything."""
    if pr.get("merged_at") or pr.get("state") not in (None, "open"):
        return False
    return card.state in CI_WATCH_STATES


def _iso8601(when: float | None) -> str | None:
    if when is None:
        return None
    return datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
_BRANCH_RE = re.compile(r"^devin/issue-(\d+)")


def _parse_verdicts(output: dict[str, Any] | None) -> list[Verdict]:
    if not output:
        return []
    raw = output.get("verdicts") if isinstance(output, dict) else None
    if not isinstance(raw, list):
        return []
    verdicts: list[Verdict] = []
    for item in raw:
        try:
            verdicts.append(Verdict.model_validate(item))
        except Exception:  # a malformed verdict should not sink the batch
            log.warning("skipping malformed verdict: %r", item)
    return verdicts


def _index_prs(prs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Map issue number -> PR, via `Closes #N` or the branch naming convention.

    Written on both sides deliberately: the link survives losing either the PR
    body or the branch name.
    """
    index: dict[int, dict[str, Any]] = {}
    for pr in prs:
        numbers: set[int] = set()
        body = pr.get("body") or ""
        for match in _CLOSES_RE.finditer(body):
            numbers.add(int(match.group(1)))
        branch = (pr.get("head") or {}).get("ref", "")
        branch_match = _BRANCH_RE.match(branch)
        if branch_match:
            numbers.add(int(branch_match.group(1)))
        for number in numbers:
            current = index.get(number)
            if current is None or pr["number"] > current["number"]:
                index[number] = pr
    return index
