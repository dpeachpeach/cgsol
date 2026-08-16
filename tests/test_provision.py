"""`make github-app` replaces two manual acts (mint a PAT, create a webhook) with
one object. These assert the shape of that object and the handshake that yields
it — the parts an evaluator cannot re-click their way out of if they are wrong."""

from __future__ import annotations

import json
from base64 import b64decode

import httpx
import pytest

from orchestrator.provision import (
    EVENTS,
    AppCredentials,
    build_manifest,
    env_updates,
    exchange_code,
    find_installation,
    manifest_page,
    mint_smee_channel,
    poll_for_installation,
    register_url,
)


@pytest.fixture
def credentials(rsa_keypair: tuple[str, str]) -> AppCredentials:
    """A real key: the installation poll signs a JWT with it."""
    return AppCredentials(
        app_id="123456",
        slug="cgsol-orchestrator",
        pem=rsa_keypair[0],
        webhook_secret="whsec",
        html_url="https://github.com/apps/cgsol-orchestrator",
    )


def test_manifest_asks_for_exactly_the_access_the_pipeline_uses() -> None:
    manifest = build_manifest(
        "https://smee.io/abc", "http://localhost:8765/callback", "dpeachpeach/superset-cg"
    )

    assert manifest["default_permissions"] == {
        "issues": "write",  # labels are the state
        "pull_requests": "write",
        "contents": "write",  # .cgsol/config.yaml lives in the fork
        "checks": "read",  # CI is the gate; we only read its verdict
        "metadata": "read",
    }
    assert set(manifest["default_events"]) == set(EVENTS)
    assert manifest["public"] is False
    # The app is the webhook. There is no second object to create by hand.
    assert manifest["hook_attributes"] == {"url": "https://smee.io/abc", "active": True}
    assert manifest["redirect_url"] == "http://localhost:8765/callback"
    assert manifest["url"]  # required by GitHub


def test_manifest_page_posts_the_manifest_to_the_state_bearing_url() -> None:
    manifest = build_manifest("https://smee.io/abc", "http://localhost:8765/callback", "o/r")
    action = register_url("dpeachpeach", "st4te")
    page = manifest_page(manifest, action)

    assert 'method="post"' in page
    assert f'action="{action}"' in page
    assert 'name="manifest"' in page
    # HTML-escaped, or the JSON's quotes end the attribute early.
    assert '"' not in page.split('value="')[1].split('"')[0]


def test_register_url_distinguishes_a_user_from_an_organization() -> None:
    assert register_url("dpeachpeach", "s") == "https://github.com/settings/apps/new?state=s"
    assert (
        register_url("acme", "s", org=True)
        == "https://github.com/organizations/acme/settings/apps/new?state=s"
    )


def test_smee_channel_comes_from_the_redirect_not_the_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://smee.io/newchannel"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert mint_smee_channel(client) == "https://smee.io/newchannel"


def test_callback_exchanges_the_code_for_the_apps_credentials(credentials: AppCredentials) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/app-manifests/tempcode/conversions"
        return httpx.Response(
            201,
            json={
                "id": 123456,
                "slug": "cgsol-orchestrator",
                "pem": credentials.pem,
                "webhook_secret": "whsec",
                "html_url": "https://github.com/apps/cgsol-orchestrator",
                "client_secret": "unused",
            },
        )

    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handle)
    ) as client:
        converted = exchange_code(client, "tempcode")

    assert (converted.app_id, converted.slug) == ("123456", "cgsol-orchestrator")
    assert converted.webhook_secret == "whsec"
    assert converted.pem == credentials.pem
    assert "PRIVATE KEY" not in repr(converted)  # the pem stays out of logs


def test_a_failed_exchange_is_loud() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Code expired"})

    with (
        httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handle)
        ) as client,
        pytest.raises(RuntimeError, match="Code expired"),
    ):
        exchange_code(client, "stale")


def test_env_gets_the_credentials_with_the_pem_encoded(credentials: AppCredentials) -> None:
    updates = env_updates(credentials, "https://smee.io/abc")

    assert updates["GITHUB_APP_ID"] == "123456"
    assert updates["GITHUB_APP_SLUG"] == "cgsol-orchestrator"
    assert updates["GITHUB_WEBHOOK_SECRET"] == "whsec"
    assert updates["SMEE_URL"] == "https://smee.io/abc"
    # One line, and decodable back into the key the JWT is signed with.
    assert "\n" not in updates["GITHUB_APP_PRIVATE_KEY"]
    assert b64decode(updates["GITHUB_APP_PRIVATE_KEY"]).decode() == credentials.pem


def test_installation_is_matched_by_account() -> None:
    installations = [
        {"id": 1, "account": {"login": "someone-else"}},
        {"id": 2, "account": {"login": "DPeachPeach"}},
    ]
    assert find_installation(installations, "dpeachpeach") == "2"
    assert find_installation([], "dpeachpeach") is None


def test_polling_waits_for_the_install_click_then_records_the_id(
    credentials: AppCredentials,
) -> None:
    responses = iter([[], [{"id": 7, "account": {"login": "dpeachpeach"}}]])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=next(responses))
        assert request.url.path == "/repos/dpeachpeach/superset-cg/installation"
        return httpx.Response(200, json={"id": 7})

    slept: list[float] = []
    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handle)
    ) as client:
        found = poll_for_installation(
            client,
            credentials,
            "dpeachpeach/superset-cg",
            timeout=60,
            interval=1,
            sleep=slept.append,
        )

    assert found == "7"
    assert slept == [1]


def test_polling_is_time_boxed_rather_than_hanging_forever(credentials: AppCredentials) -> None:
    """The human may never click. That is a message and an exit code, not a
    process an evaluator has to notice is stuck."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    clock = iter([0.0, 5.0, 30.0, 61.0])
    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handle)
    ) as client:
        found = poll_for_installation(
            client,
            credentials,
            "dpeachpeach/superset-cg",
            timeout=60,
            interval=1,
            sleep=lambda _: None,
            now=lambda: next(clock),
        )

    assert found is None


def test_installation_on_the_account_but_not_the_fork_does_not_count(
    credentials: AppCredentials,
) -> None:
    """`Only select repositories` without the fork selected is a 404 on every
    subsequent call; catching it here is the difference between a clear message
    now and a broken run later."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 7, "account": {"login": "dpeachpeach"}}])
        return httpx.Response(404, json={"message": "Not Found"})

    clock = iter([0.0, 61.0])
    with httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handle)
    ) as client:
        assert (
            poll_for_installation(
                client,
                credentials,
                "dpeachpeach/superset-cg",
                timeout=60,
                interval=1,
                sleep=lambda _: None,
                now=lambda: next(clock),
            )
            is None
        )


def test_the_manifest_is_valid_json_when_unescaped() -> None:
    manifest = build_manifest("https://smee.io/abc", "http://localhost:8765/callback", "o/r")
    page = manifest_page(manifest, register_url("o", "s"))
    value = page.split('value="')[1].split('">')[0]
    decoded = (
        value.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )
    assert json.loads(decoded) == manifest
