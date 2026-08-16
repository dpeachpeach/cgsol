"""Devin API client.

Two API versions, deliberately:

* **v3** (`POST /v3/organizations/{org}/sessions`) to create, because that is
  where `structured_output_schema` / `structured_output_required` live, and
  where session detail reports `acus_consumed`.
* **v1** (`GET /v1/sessions?tags=...`) to poll, because it is the one list
  endpoint that filters by tag. One tag-filtered list call replaces N per-session
  GETs: 20 sessions polled every 15s is 4,800 req/hr, the list call is 240.

v3 create has no `idempotent` flag, so idempotency is enforced here instead —
and on the right key. Before dispatching we look for a live session already
tagged with this issue and role. That is idempotent on the *target state*, which
is what we want anyway when three actors write labels concurrently.

v3 create needs a service-user key holding the org-level `UseDevinSessions`
permission; a personal key falls back to `POST /v1/sessions`, which takes the
same playbook / knowledge / tag / ACU-ceiling arguments.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.models import SessionInfo
from orchestrator.transport import build_transport

log = logging.getLogger("cgsol.devin")

#: v3 needs a service user with `org.devins.use`; a personal key gets 403/404
#: there and must create through v1 instead.
_NO_V3_ACCESS = {401, 403, 404}


class DevinClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.devin_api_base,
            headers={
                "Authorization": f"Bearer {settings.devin_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "cgsol-orchestrator",
            },
            timeout=60.0,
            transport=build_transport("devin"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- create ---------------------------------------------------------------

    async def create_session(
        self,
        prompt: str,
        *,
        tags: list[str],
        title: str | None = None,
        playbook_id: str | None = None,
        knowledge_ids: list[str] | None = None,
        max_acu_limit: float | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        structured_output_required: bool = True,
    ) -> SessionInfo:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "tags": tags,
            "resumable": True,
        }
        if title:
            payload["title"] = title
        if playbook_id:
            payload["playbook_id"] = playbook_id
        if knowledge_ids:
            payload["knowledge_ids"] = knowledge_ids
        if max_acu_limit is not None:
            # The API takes an integer ceiling; round up so a 1.5 policy is not
            # silently truncated to 1.
            payload["max_acu_limit"] = max(1, int(max_acu_limit + 0.999))
        if structured_output_schema is not None:
            payload["structured_output_schema"] = structured_output_schema
            payload["structured_output_required"] = structured_output_required

        org = self._settings.devin_org_id
        if org:
            response = await self._client.post(f"/v3/organizations/{org}/sessions", json=payload)
            if response.status_code not in _NO_V3_ACCESS:
                response.raise_for_status()
                return _from_v3(response.json())
            log.info("v3 create unavailable (%s), falling back to v1", response.status_code)

        # v1 has no `structured_output_required`; the playbooks say when to call
        # `provide_structured_output`, which is what actually makes it happen.
        payload.pop("structured_output_required", None)
        payload.pop("resumable", None)
        payload["idempotent"] = True
        response = await self._client.post("/v1/sessions", json=payload)
        response.raise_for_status()
        created = response.json()
        return await self.get_session(created["session_id"])

    async def send_message(self, session_id: str, message: str) -> None:
        await self._client.post(f"/v1/session/{session_id}/message", json={"message": message})

    # --- read -----------------------------------------------------------------

    async def list_by_tags(self, tags: list[str], limit: int = 100) -> list[SessionInfo]:
        response = await self._client.get("/v1/sessions", params={"tags": tags, "limit": limit})
        response.raise_for_status()
        return [_from_v1(item) for item in response.json().get("sessions", [])]

    async def get_session(self, session_id: str) -> SessionInfo:
        """Full detail, including ACU burn and structured output.

        v1 session detail carries structured output but not ACUs; v3 carries
        ACUs. Prefer v3 and fall back, so the client still works for orgs
        without v3 access.
        """
        org = self._settings.devin_org_id
        if org:
            response = await self._client.get(f"/v3/organizations/{org}/sessions/{session_id}")
            if response.status_code == 200:
                return _from_v3(response.json())
        response = await self._client.get(f"/v1/session/{session_id}")
        response.raise_for_status()
        return _from_v1(response.json())


def _from_v3(raw: dict[str, Any]) -> SessionInfo:
    prs = raw.get("pull_requests") or []
    pr_url = None
    if prs:
        first = prs[0]
        pr_url = first.get("pr_url") or first.get("url")
    return SessionInfo(
        session_id=raw["session_id"],
        status=raw.get("status", "unknown"),
        status_enum=raw.get("status_enum"),
        tags=raw.get("tags") or [],
        title=raw.get("title"),
        url=raw.get("url", ""),
        pr_url=pr_url,
        acus_consumed=float(raw.get("acus_consumed") or 0.0),
        structured_output=raw.get("structured_output"),
        origin=raw.get("origin"),
        playbook_id=raw.get("playbook_id"),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _from_v1(raw: dict[str, Any]) -> SessionInfo:
    pull_request = raw.get("pull_request") or {}
    return SessionInfo(
        session_id=raw["session_id"],
        status=raw.get("status", "unknown"),
        status_enum=raw.get("status_enum"),
        tags=raw.get("tags") or [],
        title=raw.get("title"),
        url=raw.get("url", ""),
        pr_url=pull_request.get("url"),
        acus_consumed=float(raw.get("acus_consumed") or 0.0),
        structured_output=raw.get("structured_output"),
        origin=raw.get("origin"),
        playbook_id=raw.get("playbook_id"),
        updated_at=str(raw.get("updated_at") or ""),
    )
