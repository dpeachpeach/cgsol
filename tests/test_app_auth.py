"""The App is a credential story: a JWT we sign, a token we cache, a PAT we
keep working. All three are cheap to get subtly wrong and expensive to notice."""

from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest

from orchestrator.appauth import InstallationTokenProvider, build_jwt, build_token_provider
from orchestrator.config import Settings
from orchestrator.github import GitHubClient

API = "https://api.github.com"


def app_settings(pem: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "replay": False,
        "github_token": "",
        "github_app_id": "123456",
        "github_app_slug": "cgsol-orchestrator",
        "github_app_private_key": b64encode(pem.encode()).decode(),
        "github_app_installation_id": "42",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def expires_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_pem_survives_a_dotenv_line_in_every_shape(rsa_keypair: tuple[str, str]) -> None:
    pem, _ = rsa_keypair
    assert app_settings(pem).github_app_private_key_pem == pem
    assert Settings(github_app_private_key=pem).github_app_private_key_pem == pem
    escaped = Settings(github_app_private_key=pem.replace("\n", "\\n"))
    assert escaped.github_app_private_key_pem == pem
    with pytest.raises(ValueError):
        _ = Settings(github_app_private_key="not a key").github_app_private_key_pem


def test_jwt_is_rs256_backdated_and_expires_inside_ten_minutes(
    rsa_keypair: tuple[str, str],
) -> None:
    pem, public = rsa_keypair
    token = build_jwt("123456", pem, now=1_000_000)
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False})

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert claims["iss"] == "123456"
    assert claims["iat"] < 1_000_000  # backdated against clock drift
    assert 0 < claims["exp"] - 1_000_000 <= 600  # GitHub rejects anything longer


async def test_token_is_minted_with_the_jwt_and_reused_until_it_nearly_expires(
    rsa_keypair: tuple[str, str],
) -> None:
    pem, public = rsa_keypair
    minted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/42/access_tokens"
        assertion = request.headers["authorization"].removeprefix("Bearer ")
        # Signed by the app's key, or GitHub would not accept it either.
        claims = jwt.decode(assertion, public, algorithms=["RS256"])
        assert claims["iss"] == "123456"
        minted.append(assertion)
        return httpx.Response(
            201, json={"token": f"ghs_{len(minted)}", "expires_at": expires_in(3600)}
        )

    provider = InstallationTokenProvider(
        app_settings(pem),
        client=httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handle)),
    )

    assert await provider.token() == "ghs_1"
    assert await provider.token() == "ghs_1"  # cached, not one mint per call
    assert len(minted) == 1


async def test_token_is_refreshed_before_it_expires(rsa_keypair: tuple[str, str]) -> None:
    """A poller that runs for hours must never present an expired token; the
    skew window is what buys the refresh before the 401 rather than after it."""
    pem, _ = rsa_keypair
    ttl = iter([120, 3600])  # the first token dies inside the 300s skew window
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            201, json={"token": f"ghs_{calls}", "expires_at": expires_in(next(ttl))}
        )

    provider = InstallationTokenProvider(
        app_settings(pem),
        client=httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handle)),
    )
    await provider.token()
    await provider.token()
    assert calls == 2

    await provider.token()
    assert calls == 2  # the second token is good for an hour


async def test_a_failed_mint_says_why_without_quoting_the_key(rsa_keypair: tuple[str, str]) -> None:
    pem, _ = rsa_keypair

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Integration not found"})

    provider = InstallationTokenProvider(
        app_settings(pem),
        client=httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(RuntimeError) as caught:
        await provider.token()
    assert "Integration not found" in str(caught.value)
    assert "PRIVATE KEY" not in str(caught.value)


async def test_missing_installation_id_is_an_error_not_an_anonymous_call(
    rsa_keypair: tuple[str, str],
) -> None:
    pem, _ = rsa_keypair
    settings = app_settings(pem, github_app_installation_id="")
    provider = InstallationTokenProvider(
        settings,
        client=httpx.AsyncClient(
            base_url=API, transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ),
    )
    with pytest.raises(RuntimeError, match="GITHUB_APP_INSTALLATION_ID"):
        await provider.token()


async def test_every_github_call_carries_the_installation_token(
    rsa_keypair: tuple[str, str],
) -> None:
    pem, _ = rsa_keypair
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "ghs_live", "expires_at": expires_in(3600)})
        return httpx.Response(200, text="contents", headers={"content-type": "text/plain"})

    settings = app_settings(pem)
    transport = httpx.MockTransport(handle)
    provider = InstallationTokenProvider(
        settings, client=httpx.AsyncClient(base_url=API, transport=transport)
    )
    client = GitHubClient(settings, tokens=provider)
    client._client = httpx.AsyncClient(base_url=settings.github_api_url, transport=transport)

    await client.get_file("README.md")

    assert seen[-1] == "Bearer ghs_live"


def test_no_app_credentials_means_the_pat_is_still_the_credential() -> None:
    """Replay and the live run both hang off GITHUB_TOKEN; this must not be a
    flag day, so the app path only engages when it is fully configured."""
    pat = Settings(replay=False, github_token="ghp_pat")
    assert build_token_provider(pat) is None
    client = GitHubClient(pat)
    assert client._client.headers["authorization"] == "Bearer ghp_pat"

    half_configured = Settings(replay=False, github_token="ghp_pat", github_app_id="123456")
    assert build_token_provider(half_configured) is None


def test_replay_never_mints_anything(rsa_keypair: tuple[str, str]) -> None:
    """Replay makes no outbound calls by construction; minting a token would be
    the one call that escaped the transport."""
    pem, _ = rsa_keypair
    settings = app_settings(pem, replay=True)
    assert build_token_provider(settings) is None
