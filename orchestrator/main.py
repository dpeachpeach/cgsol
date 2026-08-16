"""HTTP surface.

The frontend reads only from here, never from GitHub or Devin directly: a
write-scoped PAT in a browser is readable in devtools, the Devin API will not
CORS, the issue↔session join would have to exist twice, and rate limit would
scale with the number of open tabs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import yaml
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from orchestrator.config import get_settings
from orchestrator.labels import State
from orchestrator.metrics import compute
from orchestrator.models import TriageEstimate
from orchestrator.service import CONFIG_PATH, Orchestrator
from orchestrator.webhooks import verify_signature

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("cgsol.api")

orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="orchestrator not started")
    return orchestrator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global orchestrator
    orchestrator = Orchestrator()
    await orchestrator.start()
    try:
        yield
    finally:
        await orchestrator.stop()
        orchestrator = None


app = FastAPI(title="cgsol orchestrator", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- health -------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> Response:
    """Blocks until the first sync has completed.

    Deliberate: the frontend should never render an empty board that looks like a
    bug when it is really a cold start.
    """
    if orchestrator is None or not orchestrator.store.first_sync.is_set():
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse(
        {
            "status": "ok",
            "mode": "replay" if orchestrator.settings.replay else "live",
            "issues": len(orchestrator.store.cards()),
            "subscribers": orchestrator.store.subscriber_count,
        }
    )


# --- webhook ------------------------------------------------------------------


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> Response:
    service = get_orchestrator()
    body = await request.body()
    if not verify_signature(service.settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad signature")
    if x_github_delivery and service.dedup.seen(x_github_delivery):
        return JSONResponse({"status": "duplicate"})
    payload = json.loads(body or b"{}")
    # Return fast, work later: GitHub times webhooks out at 10s and retries.
    background.add_task(service.handle_event, x_github_event or "", payload)
    return JSONResponse({"status": "accepted", "event": x_github_event})


# --- reads --------------------------------------------------------------------


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    service = get_orchestrator()
    return service.store.snapshot()


@app.get("/api/issues/{number}")
async def api_issue(number: int) -> dict[str, Any]:
    service = get_orchestrator()
    card = service.store.card(number)
    if card is None:
        raise HTTPException(status_code=404, detail="unknown issue")
    payload = card.model_dump(mode="json")
    if card.session is not None:
        payload["session_url"] = (
            card.session.url
            or f"{service.settings.devin_app_base}/sessions/{card.session.session_id}"
        )
    return payload


@app.get("/api/metrics")
async def api_metrics() -> dict[str, Any]:
    service = get_orchestrator()
    computed = compute(service.store.cards(), service.store.sessions(), service.metrics.escalations)
    computed["series"] = service.metrics.series()
    return computed


@app.get("/api/events")
async def api_events(request: Request) -> EventSourceResponse:
    service = get_orchestrator()

    async def stream() -> AsyncIterator[dict[str, str]]:
        async with service.store.subscribe() as queue:
            yield {"event": "snapshot", "data": json.dumps(service.store.snapshot())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": message["event"], "data": json.dumps(message["data"])}

    return EventSourceResponse(stream())


# --- writes -------------------------------------------------------------------


class StateChange(BaseModel):
    state: State


@app.post("/api/issues/{number}/state")
async def api_set_state(number: int, change: StateChange) -> dict[str, Any]:
    """Writes go straight through to GitHub, never queued behind a poll."""
    service = get_orchestrator()
    card = service.store.card(number)
    if card is None:
        raise HTTPException(status_code=404, detail="unknown issue")
    await service.github.set_state(number, change.state, card.labels)
    card.state = change.state
    card.meta.human_turns += 1
    await service.store.publish(
        "issue.state", {"issue": number, "to": change.state.value, "by": "human"}
    )
    return card.model_dump(mode="json")


@app.post("/api/triage")
async def api_triage(estimate: bool = False) -> dict[str, Any]:
    service = get_orchestrator()
    result = await service.triage_all(estimate_only=estimate)
    return result.model_dump() if isinstance(result, TriageEstimate) else result


@app.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    service = get_orchestrator()
    remote = await service.github.get_file(CONFIG_PATH)
    return {
        "path": CONFIG_PATH,
        "repo": service.settings.github_repo,
        "remote": yaml.safe_load(remote) if remote else None,
        "next_chunk_at": service.next_chunk_at,
        "effective": {
            "triage_mode": service.settings.triage_mode.value,
            "triage_interval_seconds": service.settings.triage_interval_seconds,
            "confidence_threshold": service.settings.confidence_threshold,
            "max_ci_rounds": service.settings.max_ci_rounds,
            "max_concurrent_workers": service.settings.max_concurrent_workers,
            "batch_window_seconds": service.settings.batch_window_seconds,
            "scout_batch_max": service.settings.scout_batch_max,
        },
    }


@app.put("/api/config")
async def api_put_config(config: dict[str, Any]) -> dict[str, Any]:
    """Settings are written to the fork, not to local disk — same bus as
    everything else, and the change is reviewable in git history."""
    service = get_orchestrator()
    body = yaml.safe_dump(config, sort_keys=True)
    await service.github.put_file(CONFIG_PATH, body, "chore(cgsol): update orchestrator config")
    applied = await service.load_remote_config()
    return {"written": CONFIG_PATH, "applied": applied}


# --- static -------------------------------------------------------------------

_dist = os.environ.get("FRONTEND_DIST", "frontend/dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "orchestrator.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")) and settings.live,
    )


if __name__ == "__main__":
    main()
