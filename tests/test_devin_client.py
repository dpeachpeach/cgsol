"""The v3 org endpoints are the target; v1 is the no-org fallback.

v3 needs a service user with `UseDevinSessions`. Without one (no `DEVIN_ORG_ID`,
or a personal key that 403s), the pipeline still has to run — with no ACU burn
reported rather than a wrong one.
"""

from __future__ import annotations

import json

import httpx

from orchestrator.config import Settings
from orchestrator.devin import DevinClient

V1_DETAIL = {
    "session_id": "devin-1",
    "status": "working",
    "tags": ["issue:7"],
    "url": "https://app.devin.ai/sessions/1",
}


def client_with(handler: httpx.MockTransport, org: str | None = "org-x") -> DevinClient:
    settings = Settings(replay=True, devin_org_id=org or "")
    client = DevinClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.devin_api_base, transport=handler)
    return client


async def test_create_uses_v3_org_endpoint() -> None:
    seen: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/organizations/org-x/sessions"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"session_id": "devin-2", "status": "running", "acus_consumed": 0.4},
        )

    devin = client_with(httpx.MockTransport(handle))
    session = await devin.create_session(
        "do the thing",
        tags=["issue:7"],
        max_acu_limit=1.5,
        structured_output_schema={"type": "object"},
    )

    assert (session.session_id, session.acus_consumed) == ("devin-2", 0.4)
    assert seen[0]["max_acu_limit"] == 2  # 1.5 rounds up, never truncates to 1
    assert seen[0]["structured_output_required"] is True


async def test_create_falls_back_to_v1_when_v3_forbidden() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            seen.append((path, json.loads(request.content)))
        if path.startswith("/v3/"):
            return httpx.Response(403, json={"detail": "Unauthorized"})
        if path == "/v1/sessions":
            return httpx.Response(200, json={"session_id": "devin-1", "url": "u"})
        return httpx.Response(200, json=V1_DETAIL)

    devin = client_with(httpx.MockTransport(handle))
    session = await devin.create_session("do the thing", tags=["issue:7"])

    assert session.session_id == "devin-1"
    v1_payload = next(payload for path, payload in seen if path == "/v1/sessions")
    assert v1_payload["idempotent"] is True
    assert "structured_output_required" not in v1_payload


async def test_list_filters_tags_client_side_and_maps_v3_status() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/organizations/org-x/sessions"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "session_id": "3",
                        "status": "running",
                        "status_detail": "waiting_for_user",
                        "tags": ["issue:7", "role:worker"],
                        "acus_consumed": 1.2,
                    },
                    {"session_id": "devin-4", "status": "running", "tags": ["other"]},
                ]
            },
        )

    devin = client_with(httpx.MockTransport(handle))
    sessions = await devin.list_by_tags(["issue:7"])

    assert [s.session_id for s in sessions] == ["devin-3"]  # the untagged one is dropped
    assert sessions[0].waiting and not sessions[0].terminal
