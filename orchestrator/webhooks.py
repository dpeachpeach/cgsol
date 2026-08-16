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
from typing import Any

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

    async def flush_now(self) -> None:
        async with self._lock:
            batch, self._pending = self._pending, set()
        if batch:
            await self._flush(batch)

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
