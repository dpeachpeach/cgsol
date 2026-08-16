"""`make github-app` — create the GitHub App this orchestrator authenticates as.

The thing being removed here is human setup. Before this, a run needed a PAT
minted by hand in the browser and a webhook created by hand in the repo's
settings, with its secret copied into `.env` — three one-off acts that no
evaluator can reproduce and that leave a standing credential behind.

A GitHub App collapses all three into one object: it is the identity, the
webhook endpoint and the webhook secret at once, and what it hands back is a
private key the orchestrator uses to mint hour-long installation tokens for
itself. The manifest flow (see the `docs/` link below) is what lets this script
create that object without anyone filling in a settings form:

    smee channel  ->  manifest POSTed to github.com/settings/apps/new
                  ->  GitHub redirects back with a temporary code
                  ->  POST /app-manifests/{code}/conversions  ->  id, pem, secret
                  ->  the human installs the app on the fork
                  ->  GET /app/installations  ->  installation id

Two clicks remain and cannot be removed: "Create GitHub App" and "Install".
GitHub requires a human to own an app and to consent to its installation.
Everything on either side of those two clicks is this file.

https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import secrets
import sys
import threading
import time
import webbrowser
from base64 import b64encode
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from orchestrator.appauth import build_jwt
from orchestrator.bootstrap import write_env
from orchestrator.config import get_settings

log = logging.getLogger("cgsol.provision")

SMEE_NEW = "https://smee.io/new"
DEFAULT_APP_NAME = "cgsol-orchestrator"
DEFAULT_PORT = 8765
#: Long enough for a human to click through two GitHub pages, short enough that
#: a walked-away-from terminal does not hang a CI-less setup forever.
DEFAULT_INSTALL_TIMEOUT = 600.0

PERMISSIONS = {
    "issues": "write",
    "pull_requests": "write",
    "contents": "write",
    "checks": "read",
    "metadata": "read",
}

EVENTS = ["issues", "issue_comment", "pull_request", "check_run", "check_suite", "push"]


@dataclass(frozen=True)
class AppCredentials:
    """What `POST /app-manifests/{code}/conversions` gives back.

    `pem` is a private key: it goes to .env and nowhere else. It is deliberately
    absent from __repr__ so a stray log line or traceback cannot leak it.
    """

    app_id: str
    slug: str
    pem: str
    webhook_secret: str
    html_url: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"AppCredentials(app_id={self.app_id!r}, slug={self.slug!r}, pem=<redacted>)"

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new"


def mint_smee_channel(client: httpx.Client) -> str:
    """smee.io/new 307s to a fresh channel. The Location header is the channel."""
    response = client.get(SMEE_NEW, follow_redirects=False)
    location = response.headers.get("location", "")
    if not location:
        raise RuntimeError(f"smee.io did not redirect to a channel (status {response.status_code})")
    return location


def build_manifest(
    smee_url: str,
    callback_url: str,
    repo: str,
    name: str = DEFAULT_APP_NAME,
) -> dict[str, Any]:
    """The app registration, as data.

    `hook_attributes.url` is the smee channel rather than a public address: the
    orchestrator runs on a laptop, and the tunnel is the only endpoint GitHub can
    reach. Changing it later is a settings edit, not a re-registration.
    """
    return {
        "name": name,
        "url": f"https://github.com/{repo}",
        "description": "Orchestrates agent-driven maintenance on this fork. Labels are the state.",
        "public": False,
        "hook_attributes": {"url": smee_url, "active": True},
        "redirect_url": callback_url,
        "default_permissions": dict(PERMISSIONS),
        "default_events": list(EVENTS),
    }


def register_url(owner: str, state: str, org: bool = False) -> str:
    base = (
        f"https://github.com/organizations/{owner}/settings/apps/new"
        if org
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={state}"


def manifest_page(manifest: dict[str, Any], action: str) -> str:
    """A page whose only job is to POST the manifest.

    It has to be a POST — the manifest does not fit in a query string — so a
    form is the mechanism GitHub documents. It self-submits, with a button for
    the case where the browser blocks that.
    """
    payload = html.escape(json.dumps(manifest), quote=True)
    return f"""<!doctype html>
<html><head><title>cgsol · register GitHub App</title></head>
<body style="font-family: system-ui; margin: 4rem auto; max-width: 40rem">
  <h1>Register the cgsol orchestrator app</h1>
  <p>Click 1 of 2. GitHub will ask you to confirm the name, then create the app.</p>
  <form id="f" action="{html.escape(action, quote=True)}" method="post">
    <input type="hidden" name="manifest" value="{payload}">
    <button type="submit">Create GitHub App</button>
  </form>
  <script>document.getElementById("f").submit();</script>
</body></html>"""


def exchange_code(client: httpx.Client, code: str) -> AppCredentials:
    """Trade the temporary code for the app's permanent credentials.

    One hour to do this, per GitHub, and the code is single-use.
    """
    response = client.post(
        f"/app-manifests/{code}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"manifest conversion failed: {response.status_code} {response.text}")
    payload = response.json()
    return AppCredentials(
        app_id=str(payload["id"]),
        slug=str(payload["slug"]),
        pem=str(payload["pem"]),
        webhook_secret=str(payload.get("webhook_secret") or ""),
        html_url=str(payload.get("html_url") or ""),
    )


def env_updates(credentials: AppCredentials, smee_url: str) -> dict[str, str]:
    """.env lines. The pem is base64'd: a PEM's newlines do not survive a
    dotenv line, and encoding it keeps it from being half-printed by accident."""
    updates = {
        "GITHUB_APP_ID": credentials.app_id,
        "GITHUB_APP_SLUG": credentials.slug,
        "GITHUB_APP_PRIVATE_KEY": b64encode(credentials.pem.encode()).decode(),
    }
    if credentials.webhook_secret:
        updates["GITHUB_WEBHOOK_SECRET"] = credentials.webhook_secret
    if smee_url:
        updates["SMEE_URL"] = smee_url
    return updates


def find_installation(installations: list[dict[str, Any]], owner: str) -> str | None:
    for installation in installations:
        account = installation.get("account") or {}
        if str(account.get("login", "")).lower() == owner.lower():
            return str(installation["id"])
    return None


def poll_for_installation(
    client: httpx.Client,
    credentials: AppCredentials,
    repo: str,
    timeout: float = DEFAULT_INSTALL_TIMEOUT,
    interval: float = 5.0,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> str | None:
    """Wait for the human's second click. Returns the installation id, or None
    when the time box expires — never blocks forever."""
    owner = repo.split("/", 1)[0]
    deadline = now() + timeout
    while True:
        headers = {
            "Authorization": f"Bearer {build_jwt(credentials.app_id, credentials.pem)}",
            "Accept": "application/vnd.github+json",
        }
        response = client.get("/app/installations", headers=headers)
        if response.status_code == 200:
            installation_id = find_installation(list(response.json()), owner)
            if installation_id and _covers_repo(client, headers, repo, installation_id):
                return installation_id
        if now() >= deadline:
            return None
        sleep(interval)


def _covers_repo(
    client: httpx.Client, headers: dict[str, str], repo: str, installation_id: str
) -> bool:
    """An installation on the right account can still exclude the fork, which
    fails later as a 404 on every call. Ask directly instead."""
    response = client.get(f"/repos/{repo}/installation", headers=headers)
    if response.status_code != 200:
        return False
    return str(response.json().get("id", "")) == installation_id


class _CallbackServer:
    """Serves exactly two pages: the manifest form, and GitHub's redirect back."""

    def __init__(self, manifest: dict[str, Any], action: str, state: str) -> None:
        self.manifest = manifest
        self.action = action
        self.state = state
        self.code: str | None = None
        self.error: str | None = None
        self.done = threading.Event()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                log.debug(format, *args)

            def _send(self, status: int, body: str) -> None:
                encoded = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path in {"", "/"}:
                    self._send(200, manifest_page(flow.manifest, flow.action))
                    return
                if parsed.path != "/callback":
                    self._send(404, "<h1>not here</h1>")
                    return
                query = parse_qs(parsed.query)
                state = (query.get("state") or [""])[0]
                code = (query.get("code") or [""])[0]
                if state != flow.state:
                    flow.error = "state mismatch"
                elif not code:
                    flow.error = "no code in callback"
                else:
                    flow.code = code
                self._send(
                    200 if flow.error is None else 400,
                    "<h1>Registered.</h1><p>Back to the terminal.</p>"
                    if flow.error is None
                    else f"<h1>Failed: {html.escape(flow.error)}</h1>",
                )
                flow.done.set()

        return Handler


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="", help="owner/name; defaults to $GITHUB_REPO")
    parser.add_argument("--name", default=DEFAULT_APP_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--org", default="", help="register under this organization account")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--timeout", type=float, default=DEFAULT_INSTALL_TIMEOUT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    repo = args.repo or settings.github_repo
    smee_url = settings.smee_url
    if not smee_url:
        with httpx.Client(timeout=30.0) as plain:
            smee_url = mint_smee_channel(plain)
    print(f"webhook channel: {smee_url}")

    state = secrets.token_urlsafe(24)
    callback = f"http://localhost:{args.port}/callback"
    manifest = build_manifest(smee_url, callback, repo, name=args.name)
    action = register_url(args.org or repo.split("/", 1)[0], state, org=bool(args.org))

    flow = _CallbackServer(manifest, action, state)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), flow.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    start_url = f"http://localhost:{args.port}/"
    print(f"\nClick 1 of 2 — create the app: {start_url}")
    if not args.no_browser:
        webbrowser.open(start_url)

    try:
        if not flow.done.wait(timeout=args.timeout):
            print("timed out waiting for the registration callback; re-run `make github-app`")
            return 1
    finally:
        server.shutdown()
    if flow.error or not flow.code:
        print(f"registration failed: {flow.error or 'no code'}")
        return 1

    with httpx.Client(base_url=settings.github_api_url, timeout=30.0) as api:
        credentials = exchange_code(api, flow.code)
        print(f"app {credentials.slug} (id {credentials.app_id}) created; private key stored")
        write_env(env_updates(credentials, smee_url), Path(args.env))

        print(f"\nClick 2 of 2 — install it on {repo}: {credentials.install_url}")
        if not args.no_browser:
            webbrowser.open(credentials.install_url)
        installation_id = poll_for_installation(api, credentials, repo, timeout=args.timeout)

    if installation_id is None:
        print(
            f"\nNo installation on {repo} after {args.timeout:.0f}s. The app exists and its"
            f" credentials are in {args.env}; nothing is lost. Install it at"
            f" {credentials.install_url}, then copy the id from the URL of"
            " github.com/settings/installations into GITHUB_APP_INSTALLATION_ID."
        )
        return 1
    write_env({"GITHUB_APP_INSTALLATION_ID": installation_id}, Path(args.env))
    print(f"installation {installation_id} recorded. `make live` now runs without a PAT.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
