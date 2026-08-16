"""Polling and reconciliation.

Webhooks give latency; polling gives correctness. Both run: an event moves an
issue within a second, and a full sweep every few minutes repairs whatever the
event stream dropped, duplicated, or delivered while the process was restarting.
That is the intended pattern, not a fallback hack — a webhook is a hint that
state changed, never the record of what it changed to.

Polling is one tag-filtered list call per cycle, not one GET per session, and
terminal sessions are never polled again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

from orchestrator.config import Settings
from orchestrator.devin import DevinClient
from orchestrator.dispatch import Dispatcher
from orchestrator.github import GitHubClient
from orchestrator.labels import State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import IssueMeta, SessionInfo, Verdict
from orchestrator.state import Store

log = logging.getLogger("cgsol.poller")


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
        self._handled_terminal: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        await self.reconcile()
        self.store.first_sync.set()
        self._tasks = [
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
                await self.store.publish("tick", {"synced_at": time.time()})
            except asyncio.CancelledError:
                raise
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
            except Exception:
                log.exception("reconcile failed")

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
            if session.terminal and session.session_id not in self._handled_terminal:
                self._handled_terminal.add(session.session_id)
                await self._on_terminal(session)
            elif previous is None and session.origin == "automation":
                # Discovered, not dispatched: an Automation created this session
                # and nobody told us. Convergence, not notification.
                await self._adopt(session)

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

    async def _on_terminal(self, summary: SessionInfo) -> None:
        detail = await self.devin.get_session(summary.session_id)
        self.store.upsert_session(detail)
        output: dict[str, Any] | None = detail.structured_output
        role = detail.role or summary.role

        if role == "scout":
            verdicts = _parse_verdicts(output)
            if not verdicts:
                log.warning("scout %s produced no verdicts", detail.session_id)
                return
            await self.dispatcher.apply_verdicts(verdicts)
            await self.store.publish("scout.finished", {"verdicts": len(verdicts)})
            return

        number = detail.issue_number
        card = self.store.card(number) if number is not None else None
        if card is None:
            return
        card.meta.acus = max(card.meta.acus, detail.acus_consumed)
        if detail.status == "error":
            await self.dispatcher.escalate(card, "session-error", State.DEVIN_BLOCKED)
            return
        if role == "ci-fix":
            await self.dispatcher.on_ci_fix_finished(card, output)
        elif role == "worker":
            await self.dispatcher.on_worker_finished(card, output)

    # --- reconcile ------------------------------------------------------------

    async def reconcile(self) -> None:
        """Full sweep: GitHub is authoritative, this makes the projection agree."""
        issues = await self.github.list_issues(state="all", limit=100)
        prs = await self.github.list_open_prs()
        pr_by_issue = _index_prs(prs)

        for issue in issues:
            meta = None
            existing = self.store.card(issue.number)
            if existing is None or not existing.meta.session_id:
                found = await self.github.find_meta_comment(issue.number)
                meta = found[1] if found else IssueMeta()
            card = self.store.upsert_issue(issue, meta)
            pr = pr_by_issue.get(issue.number)
            if pr is not None:
                card.pr_number = pr["number"]
                card.meta.pr_url = pr["html_url"]
                card.pr_merged = bool(pr.get("merged_at"))
                checks = await self.github.check_runs_for_ref(pr["head"]["sha"])
                self.store.set_checks(issue.number, checks)
                await self.dispatcher.evaluate_ci(card, checks, card.pr_merged)
            if issue.state == "closed" and card.state not in (State.DONE, State.DEVIN_DECLINED):
                card.state = State.DONE

        await self.poll_sessions()
        self.metrics.sample(self.store.cards(), self.store.sessions())
        await self.store.publish("reconciled", self.store.snapshot())


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
