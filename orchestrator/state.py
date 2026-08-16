"""In-memory projection.

There is no database, on purpose. GitHub holds the state (labels + the metadata
blob) and Devin holds session state; anything here is a cache of those two. Cold
start is two API calls and about two seconds, which is cheaper than the
consistency bugs a second source of truth would buy at this scale.

The one thing that is not derivable by re-reading the APIs is the metrics
time-series, which lives in a ring buffer (and is optionally committed to the
fork for durable history without breaking the rule).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestrator.labels import State, Tier, state_of, tier_of
from orchestrator.models import CheckRun, Issue, IssueCard, IssueMeta, SessionInfo


class Store:
    def __init__(self) -> None:
        self._cards: dict[int, IssueCard] = {}
        self._sessions: dict[str, SessionInfo] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self.first_sync = asyncio.Event()
        self.started_at = time.time()
        #: What GitHub last said is left of the hourly budget. Rendered, so an
        #: hour that is about to run out is visible before the board freezes.
        self.budget: dict[str, Any] = {}

    # --- projection -----------------------------------------------------------

    def upsert_issue(self, issue: Issue, meta: IssueMeta | None = None) -> IssueCard:
        card = self._cards.get(issue.number)
        if card is None:
            card = IssueCard(
                number=issue.number,
                title=issue.title,
                html_url=issue.html_url,
            )
            self._cards[issue.number] = card
        card.title = issue.title
        card.html_url = issue.html_url
        card.created_at = issue.created_at or card.created_at
        card.filed_at = issue.filed_at or card.filed_at
        card.labels = issue.labels
        card.state = state_of(issue.labels)
        card.tier = tier_of(issue.labels)
        if meta is not None:
            card.meta = meta
        card.last_synced = time.time()
        return card

    def upsert_session(self, session: SessionInfo) -> None:
        self._sessions[session.session_id] = session
        number = session.issue_number
        if number is None:
            return
        card = self._cards.get(number)
        if card is None:
            return
        card.session = session
        if session.pr_url and not card.meta.pr_url:
            card.meta.pr_url = session.pr_url
        card.meta.record_spend(session.session_id, session.acus_consumed)
        card.last_synced = time.time()

    def set_checks(self, number: int, checks: list[CheckRun]) -> None:
        card = self._cards.get(number)
        if card is not None:
            card.checks = checks
            card.last_synced = time.time()

    def retain(self, numbers: set[int]) -> list[int]:
        """Drop cards for issues a full sweep no longer sees. Deleted, made a
        discussion, transferred: whatever happened, GitHub is the record."""
        gone = [number for number in self._cards if number not in numbers]
        for number in gone:
            del self._cards[number]
        return gone

    def card(self, number: int) -> IssueCard | None:
        return self._cards.get(number)

    def cards(self) -> list[IssueCard]:
        return sorted(self._cards.values(), key=lambda card: card.number)

    def sessions(self) -> list[SessionInfo]:
        return list(self._sessions.values())

    def session(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def active_worker_count(self) -> int:
        return sum(
            1
            for session in self._sessions.values()
            if session.role in ("worker", "ci-fix") and not session.terminal
        )

    def session_for_issue(self, number: int, role: str) -> SessionInfo | None:
        for session in self._sessions.values():
            if session.issue_number == number and session.role == role and not session.terminal:
                return session
        return None

    def by_state(self, state: State) -> list[IssueCard]:
        return [card for card in self.cards() if card.state is state]

    def snapshot(self) -> dict[str, Any]:
        return {
            "cards": [card.model_dump(mode="json") for card in self.cards()],
            "counts": {state.value: len(self.by_state(state)) for state in State},
            "tiers": {
                tier.value: len([c for c in self.cards() if c.tier is tier]) for tier in Tier
            },
            "active_sessions": self.active_worker_count(),
            "budget": self.budget or None,
            "synced_at": time.time(),
        }

    # --- fan-out --------------------------------------------------------------

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data, "ts": time.time()}
        async with self._lock:
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for queue in self._subscribers:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    dead.append(queue)
            for queue in dead:
                self._subscribers.discard(queue)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
