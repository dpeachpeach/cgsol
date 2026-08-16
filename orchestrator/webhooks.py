"""Webhook plumbing: verify, deduplicate, filter, debounce.

The sender filter is not a nicety. Devin writes labels too, and so does this
server; without filtering on `sender.login` the state machine feeds its own
events back to itself and the first triage batch never stops.

Three identities, not two, once the orchestrator authenticates as a GitHub App:
the human, Devin (`devin-ai-integration[bot]`) and the orchestrator itself
(`<app-slug>[bot]`). Under a PAT the orchestrator was indistinguishable from the
human who minted it; as an App it is a bot like any other, which a blanket
"anything ending in [bot] is not a human" test would silently over-filter — the
seeded backlog is labelled `needs-triage` by *us*, and a filter that drops our
own label writes drops the event that starts the whole pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

log = logging.getLogger("cgsol.webhooks")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret:
        return True  # unset secret means an unsecured local/replay run
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.removeprefix("sha256="))


class DeliveryDedup:
    """TTL set over `X-GitHub-Delivery`. Losing it on restart is harmless — the
    reconciler converges anyway, and a duplicate event is idempotent by design."""

    def __init__(self, ttl: float = 900.0) -> None:
        self._ttl = ttl
        self._seen: dict[str, float] = {}

    def seen(self, delivery_id: str) -> bool:
        now = time.time()
        if len(self._seen) > 4096:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}
        previous = self._seen.get(delivery_id)
        self._seen[delivery_id] = now
        return previous is not None and now - previous < self._ttl


class Debouncer:
    """Collapse a burst of events into one batch.

    Seeding 20 issues produces 20 webhooks in about three seconds. Each one
    should not become a triage session; one batch should.
    """

    def __init__(self, window: float, flush: Callable[[set[int]], Awaitable[None]]) -> None:
        self._window = window
        self._flush = flush
        self._pending: set[int] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def add(self, issue_number: int) -> None:
        async with self._lock:
            self._pending.add(issue_number)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())

    async def flush_now(self) -> set[int]:
        """Returns what it flushed, so a caller can say what it dispatched."""
        async with self._lock:
            batch, self._pending = self._pending, set()
            timer, self._task = self._task, None
        # A flush leaves the timer with nothing to collapse, so it is cancelled
        # rather than left to wake up on an empty set.
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        if batch:
            await self._flush(batch)
        return batch

    async def _run(self) -> None:
        await asyncio.sleep(self._window)
        await self.flush_now()

    @property
    def pending(self) -> set[int]:
        return set(self._pending)


def sender_login(payload: dict[str, Any]) -> str:
    return str((payload.get("sender") or {}).get("login", ""))


def is_bot_sender(payload: dict[str, Any], bot_logins: list[str]) -> bool:
    sender = sender_login(payload)
    if not sender:
        return False
    if sender in bot_logins:
        return True
    return sender.endswith("[bot]")


def is_self_sender(payload: dict[str, Any], self_login: str) -> bool:
    """Was this event authored by the orchestrator's own GitHub App identity?

    Empty `self_login` (PAT mode) means we cannot tell, and answering "no" keeps
    the pre-App behaviour exactly.
    """
    return bool(self_login) and sender_login(payload) == self_login


#: Workers announce progress with a machine-readable prefix so a progress
#: comment is distinguishable from prose without asking GitHub anything.
PROGRESS_PREFIX = "CGSOL_PROGRESS:"
PROGRESS_PHASES = {"drafting-pr", "pr-opened"}


class Progress(NamedTuple):
    issue: int
    phase: str
    message: str
    at: str
    comment_id: int


def parse_progress(payload: dict[str, Any]) -> Progress | None:
    """A worker's progress comment, read entirely out of the delivery.

    Everything the card needs is already in the payload, which is the point:
    the orchestrator learns that work started without spending a read on it.
    Anything unrecognised is None, and an unknown phase is dropped rather than
    rendered, so a worker cannot invent board states by writing prose.
    """
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    body = str(comment.get("body") or "").strip()
    if not body.startswith(PROGRESS_PREFIX):
        return None
    number = (payload.get("issue") or {}).get("number")
    comment_id = comment.get("id")
    if not isinstance(number, int) or not isinstance(comment_id, int):
        return None
    remainder = body.removeprefix(PROGRESS_PREFIX).strip()
    phase, _, message = remainder.partition(" ")
    if phase not in PROGRESS_PHASES:
        return None
    return Progress(
        issue=number,
        phase=phase,
        message=message.strip(),
        at=str(comment.get("created_at") or ""),
        comment_id=comment_id,
    )


def classify_sender(payload: dict[str, Any], bot_logins: list[str], self_login: str) -> str:
    """ "self" | "bot" | "human". Callers want different answers per event:
    our own writes must not count as human intent, but they must still be able
    to start triage, and only Devin's and third parties' writes are inert.
    """
    if is_self_sender(payload, self_login):
        return "self"
    if is_bot_sender(payload, bot_logins):
        return "bot"
    return "human"
