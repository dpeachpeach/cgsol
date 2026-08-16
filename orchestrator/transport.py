"""Record/replay transport.

Every outbound HTTP call — GitHub and Devin alike — goes through one httpx
transport. In `record` mode it writes each exchange to a cassette; in `replay`
mode it serves from one and never touches the network. Recording both APIs in
the same pass is what keeps the replayed timeline coherent: the Devin session
that flips to `finished` must line up with the GitHub PR that appears.

The cassette is a JSONL file, one exchange per line, in call order. Matching is
by (method, path, query, body-hash) with an occurrence counter, so a poll loop
that hits the same URL five times replays five different answers in sequence.
The request body is kept only as far as it stays readable — the hash in the key
is what matching uses, and a full worker prompt on one line is not something
anyone reviews.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

_REDACT_HEADERS = {"authorization", "x-hub-signature-256", "cookie", "set-cookie"}
_MAX_RECORDED_REQUEST = 400
#: Replaying these off the recording would contradict the body we are handing
#: httpx, which recomputes them.
_DERIVED_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}


def _body_hash(content: bytes) -> str:
    if not content:
        return "-"
    return hashlib.sha256(content).hexdigest()[:12]


def _key(method: str, url: httpx.URL, content: bytes) -> str:
    return f"{method.upper()} {url.host}{url.path}?{url.query.decode()} {_body_hash(content)}"


def _elide(body: str) -> str:
    if len(body) <= _MAX_RECORDED_REQUEST:
        return body
    return body[:_MAX_RECORDED_REQUEST] + f"… [{len(body)} bytes, hashed in key]"


def _scrub(headers: httpx.Headers) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _REDACT_HEADERS else v) for k, v in headers.items()}


class CassetteMissError(RuntimeError):
    """Replay was asked for an exchange the cassette does not contain."""


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes calls through and appends them to a cassette."""

    def __init__(self, cassette: Path, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._cassette = cassette
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._lock = threading.Lock()
        cassette.parent.mkdir(parents=True, exist_ok=True)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        entry = {
            "key": _key(request.method, request.url, request.content),
            "request": {
                "method": request.method,
                "url": str(request.url),
                "headers": _scrub(request.headers),
                "body": _elide(request.content.decode("utf-8", "replace")) or None,
            },
            "response": {
                "status": response.status_code,
                "headers": _scrub(response.headers),
                "body": body.decode("utf-8", "replace"),
            },
        }
        with self._lock, self._cassette.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=body,
            request=request,
        )


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves recorded exchanges. Makes no network calls, ever."""

    def __init__(self, cassette: Path, strict: bool = False) -> None:
        self._entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._cursor: dict[str, int] = defaultdict(int)
        self._strict = strict
        self._lock = threading.Lock()
        if cassette.exists():
            for line in cassette.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._entries[entry["key"]].append(entry)

    @property
    def empty(self) -> bool:
        return not self._entries

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = _key(request.method, request.url, request.content)
        with self._lock:
            bucket = self._entries.get(key)
            if not bucket:
                # Fall back to a body-insensitive match: recorded POST bodies
                # carry timestamps that will not reproduce byte-for-byte. The
                # query stays in the key — a `since=` filter is a different
                # question with a different answer, and serving the unfiltered
                # one is worse than admitting the miss.
                loose = (
                    f"{request.method.upper()} {request.url.host}{request.url.path}"
                    f"?{request.url.query.decode()} "
                )
                candidates = [k for k in self._entries if k.startswith(loose)]
                if not candidates:
                    if self._strict:
                        raise CassetteMissError(key)
                    return httpx.Response(
                        status_code=504,
                        json={"error": "cassette-miss", "key": key},
                        request=request,
                    )
                bucket = self._entries[candidates[0]]
                key = candidates[0]
            index = min(self._cursor[key], len(bucket) - 1)
            self._cursor[key] = index + 1
            entry = bucket[index]
        recorded = entry["response"]
        body = recorded["body"].encode("utf-8")
        # Headers are part of the answer, not decoration: the client reads its
        # rate-limit budget and its clock skew off them, and a reply without a
        # `Date` puts the replay back on the container's clock.
        headers = {
            name: value
            for name, value in recorded["headers"].items()
            if name.lower() not in _DERIVED_HEADERS
        }
        headers.setdefault("content-type", "application/json")
        return httpx.Response(
            status_code=recorded["status"],
            headers=headers,
            content=body,
            request=request,
        )


def build_transport(service: str) -> httpx.AsyncBaseTransport | None:
    """Return the transport for `service` ("github" | "devin") given the mode.

    Everything that distinguishes replay from live lives here, at the socket.
    Above this line there is no `if replay:` — the clients, the reconciler and
    the state machine cannot tell the difference, which is the only way replay
    is worth anything as a test.
    """

    from orchestrator.config import get_settings
    from orchestrator.simulator import SimulatedDevinTransport, SimulatedGitHubTransport

    settings = get_settings()
    cassette = Path(settings.fixtures_dir) / f"{service}.jsonl"
    if settings.replay and settings.replay_cassette:
        return ReplayTransport(cassette, strict=True)
    if settings.replay:
        simulated: httpx.AsyncBaseTransport = (
            SimulatedGitHubTransport() if service == "github" else SimulatedDevinTransport()
        )
        # RECORD on top of REPLAY cuts the cassettes from the simulated fork
        # rather than from a live one: same exchanges, no credentials, no ACUs.
        return RecordingTransport(cassette, inner=simulated) if settings.record else simulated
    if settings.record:
        return RecordingTransport(cassette)
    return None


def cassette_path(service: str) -> Path:
    return Path(os.environ.get("FIXTURES_DIR", "fixtures")) / f"{service}.jsonl"
