"""Wiring, plus the event side of the state machine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import yaml

from orchestrator.config import Settings, TriageMode, get_settings
from orchestrator.devin import DevinClient
from orchestrator.dispatch import Dispatcher
from orchestrator.github import GitHubClient, RateLimited
from orchestrator.labels import State
from orchestrator.metrics import MetricsRegistry
from orchestrator.models import TriageEstimate
from orchestrator.poller import Poller
from orchestrator.resources import load_resources
from orchestrator.state import Store
from orchestrator.webhooks import Debouncer, DeliveryDedup, classify_sender

log = logging.getLogger("cgsol.service")

CONFIG_PATH = ".cgsol/config.yaml"


class Orchestrator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = Store()
        self.metrics = MetricsRegistry()
        self.resources = load_resources()
        self.github = GitHubClient(self.settings)
        self.devin = DevinClient(self.settings)
        self.dispatcher = Dispatcher(
            self.settings, self.github, self.devin, self.store, self.resources, self.metrics
        )
        self.poller = Poller(
            self.settings, self.github, self.devin, self.store, self.dispatcher, self.metrics
        )
        self.dedup = DeliveryDedup(self.settings.delivery_ttl_seconds)
        self.debouncer = Debouncer(self.settings.batch_window_seconds, self._flush_batch)
        self._chunk_task: asyncio.Task[None] | None = None
        self.next_chunk_at: float | None = None

    async def start(self) -> None:
        await self.poller.start()
        self._restart_chunk_loop()
        if self.settings.replay and self.settings.replay_autostart:
            # Replay has no webhook to react to. Kick the backlog through the
            # same retroactive-triage path a human would press.
            asyncio.create_task(self.triage_all())  # noqa: RUF006

    async def stop(self) -> None:
        await self._cancel_chunk_loop()
        await self.poller.stop()
        await self.github.aclose()
        await self.devin.aclose()

    # --- events ---------------------------------------------------------------

    async def handle_event(self, event: str, payload: dict[str, Any]) -> str:
        """Translate a webhook into intent, unless GitHub is out of budget.

        A dropped event costs latency, not correctness: reconciliation is the
        record, the webhook only ever a hint that something moved.
        """
        try:
            return await self._handle_event(event, payload)
        except RateLimited as limit:
            log.warning("dropped %s event: %s", event, limit)
            return "rate-limited"

    async def _handle_event(self, event: str, payload: dict[str, Any]) -> str:
        # Three identities: "self" is this orchestrator (a PAT's human login, or
        # `<app-slug>[bot]` once it authenticates as an App), "bot" is Devin and
        # anything else automated, "human" is intent. Self-authored events are
        # not human intent, but they still start triage: `make seed` files the
        # backlog under our own identity.
        origin = classify_sender(
            payload, self.settings.devin_bot_logins, self.settings.github_app_login
        )
        from_bot = origin == "bot"
        from_human = origin == "human"

        if event == "issues":
            issue = payload.get("issue") or {}
            number = issue.get("number")
            if number is None:
                return "ignored"
            action = payload.get("action")
            if action in {"opened", "reopened", "labeled", "unlabeled", "edited"}:
                labels = [label["name"] for label in issue.get("labels", [])]
                if not from_bot and State.NEEDS_TRIAGE.value in labels:
                    # Only auto mode turns an arriving issue into spend. The other
                    # modes still record it; the interval sweep or the button
                    # picks it up, because the candidate rule reads GitHub rather
                    # than a queue that a restart would lose.
                    if self.settings.triage_mode is TriageMode.AUTO:
                        await self.debouncer.add(number)
                        await self._refresh_issue(number)
                        return "queued"
                    await self._refresh_issue(number)
                    return "deferred"
                if from_human:
                    self._count_human_turn(number)
                await self._refresh_issue(number)
                return "refreshed"
            if action == "closed":
                await self._refresh_issue(number)
                return "refreshed"
            return "ignored"

        if event == "issue_comment" and from_human:
            number = (payload.get("issue") or {}).get("number")
            if number is not None:
                self._count_human_turn(number)
            return "noted"

        if event in {"check_run", "check_suite", "pull_request", "status", "workflow_run"}:
            # CI moved. The reconciler owns the evaluation; poke it early rather
            # than trusting this payload's view of which checks exist.
            await self.poller.reconcile()
            return "reconciled"

        if event == "push":
            if any(
                commit.get("modified", []) or commit.get("added", [])
                for commit in payload.get("commits", [])
                if CONFIG_PATH in (commit.get("modified", []) + commit.get("added", []))
            ):
                await self.load_remote_config()
                return "config-reloaded"
            return "ignored"

        return "ignored"

    def _count_human_turn(self, number: int) -> None:
        card = self.store.card(number)
        if card is not None and card.meta.session_id:
            card.meta.human_turns += 1

    async def _refresh_issue(self, number: int) -> None:
        issue = await self.github.get_issue(number)
        card = self.store.upsert_issue(issue)
        await self.store.publish("issue.updated", card.model_dump(mode="json"))

    async def _flush_batch(self, numbers: set[int]) -> None:
        issues = [await self.github.get_issue(number) for number in sorted(numbers)]
        for issue in issues:
            self.store.upsert_issue(issue)
        session_id = await self.dispatcher.triage(issues)
        log.info("triage batch of %d issues -> %s", len(issues), session_id)

    # --- cadence --------------------------------------------------------------

    def _restart_chunk_loop(self) -> None:
        if self._chunk_task is not None:
            self._chunk_task.cancel()
            self._chunk_task = None
        self.next_chunk_at = None
        if self.settings.triage_mode is not TriageMode.CHUNKED:
            return
        self._chunk_task = asyncio.create_task(self._chunk_loop(), name="cgsol-chunked-triage")

    async def _cancel_chunk_loop(self) -> None:
        task, self._chunk_task = self._chunk_task, None
        self.next_chunk_at = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _chunk_loop(self) -> None:
        """Spend once per interval instead of once per issue.

        The sweep re-derives its candidates from GitHub rather than draining a
        queue, so an issue that arrived while the process was down is still in
        the next chunk.
        """
        while True:
            interval = max(60.0, float(self.settings.triage_interval_seconds))
            self.next_chunk_at = time.time() + interval
            await asyncio.sleep(interval)
            try:
                result = await self.triage_all()
                log.info("chunked triage swept %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("chunked triage failed")

    # --- retroactive / manual -------------------------------------------------

    async def triage_all(self, estimate_only: bool = False) -> TriageEstimate | dict[str, Any]:
        """The retroactive-triage button routes through the same handler as a
        webhook — one code path, so the demo cannot drift from the real thing."""
        issues = await self.github.list_issues(state="open", limit=100)
        candidates = [
            issue
            for issue in issues
            if State.NEEDS_TRIAGE.value in issue.labels
            or not any(label in {s.value for s in State} for label in issue.labels)
        ]
        if estimate_only:
            return self.dispatcher.estimate(candidates)
        for issue in candidates:
            await self.debouncer.add(issue.number)
        await self.debouncer.flush_now()
        return {"queued": [issue.number for issue in candidates]}

    async def load_remote_config(self) -> dict[str, Any]:
        """Settings live in the fork, not on the orchestrator's disk. Same rule as
        everything else: no local source of truth."""
        raw = await self.github.get_file(CONFIG_PATH)
        if not raw:
            return {}
        data = yaml.safe_load(raw) or {}
        # Explicitly enumerated: remote config controls policy, never credentials
        # or endpoints. A repo file that could rewrite `devin_api_base` would be
        # a write-scoped token away from being an exfiltration primitive.
        if "confidence_threshold" in data:
            self.settings.confidence_threshold = float(data["confidence_threshold"])
        if "max_ci_rounds" in data:
            self.settings.max_ci_rounds = int(data["max_ci_rounds"])
        if "max_concurrent_workers" in data:
            self.settings.max_concurrent_workers = int(data["max_concurrent_workers"])
        if "scout_batch_max" in data:
            self.settings.scout_batch_max = int(data["scout_batch_max"])
        if "batch_window_seconds" in data:
            self.settings.batch_window_seconds = float(data["batch_window_seconds"])
        if "dry_run" in data:
            self.settings.dry_run = bool(data["dry_run"])
        cadence_changed = False
        if "triage_mode" in data:
            with contextlib.suppress(ValueError):
                mode = TriageMode(str(data["triage_mode"]))
                cadence_changed = cadence_changed or mode is not self.settings.triage_mode
                self.settings.triage_mode = mode
        if "triage_interval_seconds" in data:
            interval = float(data["triage_interval_seconds"])
            cadence_changed = cadence_changed or interval != self.settings.triage_interval_seconds
            self.settings.triage_interval_seconds = interval
        if cadence_changed:
            self._restart_chunk_loop()
        await self.store.publish("config.reloaded", data)
        return dict(data)
