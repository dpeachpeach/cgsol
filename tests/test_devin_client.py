"""Session creation degrades from v3 to v1.

v3 create needs a service-user key with the org-level `UseDevinSessions`
permission. A personal key gets 403 there, and the pipeline still has to run.
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


def client_with(handler: httpx.MockTransport) -> DevinClient:
    settings = Settings(replay=True, devin_org_id="org-x")
    client = DevinClient(settings)
    client._client = httpx.AsyncClient(base_url=settings.devin_api_base, transport=handler)
    return client


async def test_v3_forbidden_falls_back_to_v1_create() -> None:
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
    session = await devin.create_session("do the thing", tags=["issue:7"], max_acu_limit=1.5)

    assert session.session_id == "devin-1"
    v1_payload = next(payload for path, payload in seen if path == "/v1/sessions")
    assert v1_payload["idempotent"] is True
    assert v1_payload["max_acu_limit"] == 2  # 1.5 rounds up, never truncates to 1
    assert "structured_output_required" not in v1_payload


async def test_v3_create_used_when_available() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/organizations/org-x/sessions"
        return httpx.Response(
            200, json={"session_id": "devin-2", "status": "running", "acus_consumed": 0.4}
        )

    devin = client_with(httpx.MockTransport(handle))
    session = await devin.create_session("do the thing", tags=["issue:7"])
    assert (session.session_id, session.acus_consumed) == ("devin-2", 0.4)
