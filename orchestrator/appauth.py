"""GitHub App authentication: a JWT the orchestrator signs, traded for a token.

A PAT is a standing credential that somebody made by hand, in a browser, and
that nothing in this repo can reproduce. A GitHub App is an identity, a webhook
endpoint and a webhook secret in one object that `make github-app` creates from
a manifest — and its credentials are installation tokens that expire in an hour,
so the worst case of a leak has a clock on it.

Two hops:

    private key --RS256 JWT (<=10 min)--> POST /app/installations/{id}/access_tokens
                                      --> installation token (1 hour)

The token is cached and refreshed early (`github_app_token_skew_seconds`), which
matters because the poller runs for hours: refreshing on the 401 instead would
turn every expiry into a failed reconcile.

`GITHUB_TOKEN` remains the credential when no app is configured. Replay mode and
the recorded run depend on it, and a flag day for a demo is a bad trade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx
import jwt

from orchestrator.config import Settings

log = logging.getLogger("cgsol.appauth")

#: GitHub rejects a JWT with `exp` more than 10 minutes out.
JWT_TTL_SECONDS = 600
#: Clock drift allowance on `iat`; GitHub's own docs recommend backdating.
JWT_BACKDATE_SECONDS = 60


def build_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    """An app JWT. `private_key_pem` never leaves this function's caller chain."""
    issued = int(now if now is not None else time.time())
    payload = {
        "iat": issued - JWT_BACKDATE_SECONDS,
        "exp": issued + JWT_TTL_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


class InstallationTokenProvider:
    """Mints and caches the installation token every GitHub call authenticates with."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cgsol-orchestrator",
            },
            timeout=30.0,
        )
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def _now(self) -> float:
        return time.time()

    def _fresh(self) -> bool:
        return bool(self._token) and self._now < (
            self._expires_at - self._settings.github_app_token_skew_seconds
        )

    async def token(self) -> str:
        if self._fresh():
            return self._token
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock;
            # the poller and a webhook burst hit this at the same moment.
            if self._fresh():
                return self._token
            await self._refresh()
        return self._token

    async def _refresh(self) -> None:
        settings = self._settings
        installation_id = settings.github_app_installation_id
        if not installation_id:
            raise RuntimeError(
                "GITHUB_APP_INSTALLATION_ID is unset; run `make github-app` or install the app"
            )
        assertion = build_jwt(settings.github_app_id, settings.github_app_private_key_pem)
        response = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {assertion}"},
        )
        if response.status_code >= 400:
            # The body carries GitHub's reason ("integration not installed") and
            # no secret material.
            raise RuntimeError(
                f"installation token request failed: {response.status_code} {response.text}"
            )
        payload: dict[str, Any] = response.json()
        self._token = str(payload["token"])
        self._expires_at = _parse_expiry(payload.get("expires_at"))
        log.info(
            "minted installation token for installation %s, expires in %.0f min",
            installation_id,
            (self._expires_at - self._now) / 60,
        )


def _parse_expiry(value: object) -> float:
    """GitHub returns RFC 3339 UTC. Fall back to the documented one hour."""
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            log.warning("unparseable expires_at, assuming one hour")
    return time.time() + 3600


def build_token_provider(settings: Settings) -> InstallationTokenProvider | None:
    """None means "keep using GITHUB_TOKEN"."""
    if settings.replay or not settings.github_app_configured:
        return None
    return InstallationTokenProvider(settings)
