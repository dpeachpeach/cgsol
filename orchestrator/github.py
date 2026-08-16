"""GitHub client. The bus."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email.utils
import json
import logging
import re
import time
from typing import Any

import httpx

from orchestrator.appauth import InstallationTokenProvider, build_token_provider
from orchestrator.config import Settings
from orchestrator.labels import ALL_STATE_LABELS, State
from orchestrator.models import CheckRun, Issue, IssueMeta
from orchestrator.resources import load_label_definitions
from orchestrator.transport import build_transport

log = logging.getLogger("cgsol.github")

#: Requests held back from the hourly budget so a write can still land after a
#: polling loop has been greedy. A stale board recovers on the next sweep; a
#: dropped label transition does not recover at all.
WRITE_RESERVE = 100

#: Pacing below this is not worth taking. Spread over a full hour a whole
#: budget works out under a second a read, and sleeping that would slow every
#: sweep without changing whether the hour survives. Above it the arithmetic
#: says the current burn rate does not fit in the window, and it should bite.
PACE_FLOOR_SECONDS = 1.0

#: Bounds the conditional-GET cache. Comfortably more than one sweep's worth
#: of distinct URLs for a fork this size.
ETAG_CACHE_MAX = 512

META_MARKER = "devin-orchestrator:"
META_RE = re.compile(r"<!--\s*devin-orchestrator:\s*(\{.*?\})\s*-->", re.DOTALL)


def render_meta(meta: IssueMeta) -> str:
    payload = json.dumps(meta.model_dump(exclude_none=True), separators=(",", ":"), sort_keys=True)
    return f"<!-- {META_MARKER} {payload} -->"


def parse_meta(text: str) -> IssueMeta | None:
    match = META_RE.search(text or "")
    if not match:
        return None
    try:
        return IssueMeta.model_validate(json.loads(match.group(1)))
    except (ValueError, TypeError):
        return None


class RateLimited(RuntimeError):
    """The hourly budget is spent. Carries when it comes back."""

    def __init__(self, resets_at: float) -> None:
        self.resets_at = resets_at
        super().__init__(f"github rate limit exhausted; resets in {resets_at - time.time():.0f}s")


class GitHubClient:
    def __init__(self, settings: Settings, tokens: InstallationTokenProvider | None = None) -> None:
        self._settings = settings
        # An app mints a fresh installation token per hour, so its Authorization
        # header cannot be baked into the client; a PAT's can.
        self._tokens = tokens if tokens is not None else build_token_provider(settings)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cgsol-orchestrator",
        }
        if self._tokens is None and settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._client = httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=headers,
            timeout=30.0,
            transport=build_transport("github"),
        )
        self._repo = settings.github_repo
        self._write_lock = asyncio.Semaphore(4)
        # A conditional GET answered 304 is not charged to the hourly budget,
        # which is what makes polling a repo every minute affordable at all.
        self._etags: dict[str, tuple[str, bytes]] = {}
        self._remaining: int | None = None
        self._limit: int | None = None
        self._resets_at: float = 0.0
        self._last_read: float = 0.0
        #: GitHub's clock minus ours, from the `Date` header. `since=` filters
        #: are compared against their clock, not the container's.
        self._clock_skew: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._tokens is not None:
            await self._tokens.aclose()

    @property
    def rate_limited_until(self) -> float:
        """Zero while there is budget left, otherwise when it comes back."""
        if self._remaining is not None and self._remaining <= 0 and self._resets_at > time.time():
            return self._resets_at
        return 0.0

    @property
    def budget(self) -> dict[str, Any]:
        """What is left of the hour, as last seen. Empty until a request answers."""
        return {
            "remaining": self._remaining,
            "limit": self._limit,
            "reserve": WRITE_RESERVE,
            "resets_at": self._resets_at or None,
        }

    def server_time(self) -> float:
        """Now, on GitHub's clock. Falls back to ours until one answers."""
        return time.time() + self._clock_skew

    @staticmethod
    def _cache_key(path: str, params: Any) -> str:
        return f"{path}?{sorted((params or {}).items())}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        read = method.upper() == "GET"
        budget = WRITE_RESERVE + 1 if self._remaining is None else self._remaining
        if self._resets_at > time.time() and (budget <= 0 or (read and budget <= WRITE_RESERVE)):
            raise RateLimited(self._resets_at)

        if read:
            await self._pace()
        headers = dict(kwargs.pop("headers", None) or {})
        if self._tokens is not None:
            headers["Authorization"] = f"Bearer {await self._tokens.token()}"
        key = self._cache_key(path, kwargs.get("params"))
        cached = self._etags.get(key) if read else None
        if cached is not None:
            headers["If-None-Match"] = cached[0]
        if headers:
            kwargs["headers"] = headers

        response = await self._client.request(method, path, **kwargs)
        self._note_limits(response)
        if response.status_code == 403 and "secondary rate limit" in response.text.lower():
            await asyncio.sleep(5)
            response = await self._client.request(method, path, **kwargs)
            self._note_limits(response)
        if response.status_code == 403 and self.rate_limited_until:
            # A fresh process learns the budget is gone by being told so once.
            raise RateLimited(self._resets_at)
        if read and response.status_code == 304 and cached is not None:
            return httpx.Response(200, content=cached[1], request=response.request)
        etag = response.headers.get("etag")
        if read and response.status_code == 200 and etag:
            # An incremental sweep asks a different URL every time, so entries
            # that will never be matched again accumulate; oldest out first.
            if len(self._etags) >= ETAG_CACHE_MAX:
                del self._etags[next(iter(self._etags))]
            self._etags[key] = (etag, response.content)
        return response

    async def _pace(self) -> None:
        """Spend what is left evenly over what is left of the hour.

        Self-tuning rather than a fixed ceiling: with a full budget the even
        spread is under a second, which `PACE_FLOOR_SECONDS` declines to take,
        so ordinary polling is unaffected. It only bites once the arithmetic
        says the remaining budget will not cover the rest of the window — and
        then a thin hour degrades into slower polling rather than a blank board.
        """
        now = time.time()
        window = self._resets_at - now
        if self._remaining is None or window <= 0:
            return
        spacing = window / max(1, self._remaining - WRITE_RESERVE)
        if spacing < PACE_FLOOR_SECONDS:
            self._last_read = now
            return
        gap = (self._last_read + spacing) - now
        if gap > 0:
            await asyncio.sleep(min(gap, window))
        self._last_read = time.time()

    def _note_limits(self, response: httpx.Response) -> None:
        date = response.headers.get("date")
        if date:
            with contextlib.suppress(TypeError, ValueError):
                self._clock_skew = email.utils.parsedate_to_datetime(date).timestamp() - time.time()
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            self._remaining, self._resets_at = int(remaining), float(reset)
        except ValueError:
            return
        limit = response.headers.get("x-ratelimit-limit")
        if limit is not None:
            with contextlib.suppress(ValueError):
                self._limit = int(limit)
        if self._remaining <= WRITE_RESERVE:
            log.warning(
                "github budget low: %s left, resets in %.0fs",
                self._remaining,
                self._resets_at - time.time(),
            )

    # --- reads ----------------------------------------------------------------

    async def list_issues(
        self, state: str = "open", limit: int = 100, since: str | None = None
    ) -> list[Issue]:
        """`since` is an ISO-8601 timestamp: only issues touched after it.

        The cheap sweep. A repo of 30 issues costs 30 issues' worth of payload
        every time it is read in full, and almost none of them moved.
        """
        issues: list[Issue] = []
        page = 1
        while len(issues) < limit:
            params: dict[str, Any] = {"state": state, "per_page": min(100, limit), "page": page}
            if since is not None:
                params["since"] = since
            response = await self._request("GET", f"/repos/{self._repo}/issues", params=params)
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            for raw in batch:
                if "pull_request" in raw:  # the issues endpoint returns PRs too
                    continue
                issues.append(_to_issue(raw))
            if len(batch) < 100:
                break
            page += 1
        return issues[:limit]

    async def get_issue(self, number: int) -> Issue:
        response = await self._request("GET", f"/repos/{self._repo}/issues/{number}")
        response.raise_for_status()
        return _to_issue(response.json())

    async def list_open_prs(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/repos/{self._repo}/pulls", params={"state": "all", "per_page": 100}
        )
        response.raise_for_status()
        return list(response.json())

    async def check_runs_for_ref(self, ref: str) -> list[CheckRun]:
        response = await self._request(
            "GET", f"/repos/{self._repo}/commits/{ref}/check-runs", params={"per_page": 100}
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return [
            CheckRun(
                name=run["name"],
                status=run["status"],
                conclusion=run.get("conclusion"),
                details_url=run.get("details_url"),
            )
            for run in response.json().get("check_runs", [])
        ]

    async def get_file(self, path: str, ref: str | None = None) -> str | None:
        params = {"ref": ref} if ref else None
        response = await self._request(
            "GET",
            f"/repos/{self._repo}/contents/{path}",
            params=params,
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def put_file(self, path: str, content: str, message: str) -> dict[str, Any]:
        """Write a file to the fork. Settings live in the repo, not on our disk."""
        sha: str | None = None
        head = await self._request("GET", f"/repos/{self._repo}/contents/{path}")
        if head.status_code == 200:
            sha = head.json().get("sha")
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
        }
        if sha:
            payload["sha"] = sha
        async with self._write_lock:
            response = await self._request(
                "PUT", f"/repos/{self._repo}/contents/{path}", json=payload
            )
        response.raise_for_status()
        return dict(response.json())

    async def find_meta_comment(self, number: int) -> tuple[int, IssueMeta] | None:
        response = await self._request(
            "GET", f"/repos/{self._repo}/issues/{number}/comments", params={"per_page": 100}
        )
        response.raise_for_status()
        for comment in response.json():
            meta = parse_meta(comment.get("body", ""))
            if meta is not None:
                return comment["id"], meta
        return None

    # --- writes ---------------------------------------------------------------

    async def set_state(self, number: int, target: State, current_labels: list[str]) -> None:
        """Make `target` the only state label on the issue."""
        stale = [
            label for label in current_labels if label in ALL_STATE_LABELS and label != target.value
        ]
        async with self._write_lock:
            for label in stale:
                await self._request("DELETE", f"/repos/{self._repo}/issues/{number}/labels/{label}")
            if target.value not in current_labels:
                await self._request(
                    "POST",
                    f"/repos/{self._repo}/issues/{number}/labels",
                    json={"labels": [target.value]},
                )

    async def add_labels(self, number: int, labels: list[str]) -> None:
        if not labels:
            return
        async with self._write_lock:
            await self._request(
                "POST", f"/repos/{self._repo}/issues/{number}/labels", json={"labels": labels}
            )

    async def remove_label(self, number: int, label: str) -> None:
        async with self._write_lock:
            await self._request("DELETE", f"/repos/{self._repo}/issues/{number}/labels/{label}")

    async def comment(self, number: int, body: str) -> None:
        async with self._write_lock:
            await self._request(
                "POST", f"/repos/{self._repo}/issues/{number}/comments", json={"body": body}
            )

    async def upsert_meta(self, number: int, meta: IssueMeta, note: str = "") -> None:
        """Write the metadata blob, updating in place when one already exists."""
        body = (note + "\n\n" if note else "") + render_meta(meta)
        existing = await self.find_meta_comment(number)
        async with self._write_lock:
            if existing is None:
                await self._request(
                    "POST", f"/repos/{self._repo}/issues/{number}/comments", json={"body": body}
                )
            else:
                comment_id, _ = existing
                await self._request(
                    "PATCH",
                    f"/repos/{self._repo}/issues/comments/{comment_id}",
                    json={"body": body},
                )

    async def ensure_labels(self) -> int:
        """Create the taxonomy. Scripted, not clicked. Idempotent."""
        created = 0
        for name, color, description in load_label_definitions():
            response = await self._request(
                "POST",
                f"/repos/{self._repo}/labels",
                json={"name": name, "color": color, "description": description},
            )
            if response.status_code == 201:
                created += 1
            elif response.status_code == 422:  # already exists
                await self._request(
                    "PATCH",
                    f"/repos/{self._repo}/labels/{name}",
                    json={"color": color, "description": description},
                )
            else:
                response.raise_for_status()
            await asyncio.sleep(0.3)
        return created

    async def create_issue(self, title: str, body: str, labels: list[str]) -> int | None:
        response = await self._request(
            "POST",
            f"/repos/{self._repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        response.raise_for_status()
        return int(response.json()["number"])


def _to_issue(raw: dict[str, Any]) -> Issue:
    return Issue(
        number=raw["number"],
        title=raw.get("title", ""),
        body=raw.get("body") or "",
        labels=[label["name"] for label in raw.get("labels", [])],
        state=raw.get("state", "open"),
        html_url=raw.get("html_url", ""),
        created_at=raw.get("created_at", ""),
        updated_at=raw.get("updated_at", ""),
    )
