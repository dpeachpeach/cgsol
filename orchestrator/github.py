"""GitHub client. The bus."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.labels import ALL_STATE_LABELS, State
from orchestrator.models import CheckRun, Issue, IssueMeta
from orchestrator.resources import load_label_definitions
from orchestrator.transport import build_transport

log = logging.getLogger("cgsol.github")

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


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cgsol-orchestrator",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._client = httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=headers,
            timeout=30.0,
            transport=build_transport("github"),
        )
        self._repo = settings.github_repo
        self._write_lock = asyncio.Semaphore(4)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code == 403 and "secondary rate limit" in response.text.lower():
            await asyncio.sleep(5)
            response = await self._client.request(method, path, **kwargs)
        return response

    # --- reads ----------------------------------------------------------------

    async def list_issues(self, state: str = "open", limit: int = 100) -> list[Issue]:
        issues: list[Issue] = []
        page = 1
        while len(issues) < limit:
            response = await self._request(
                "GET",
                f"/repos/{self._repo}/issues",
                params={"state": state, "per_page": min(100, limit), "page": page},
            )
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
        updated_at=raw.get("updated_at", ""),
    )
