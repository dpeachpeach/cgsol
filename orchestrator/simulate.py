"""`make simulate` — replay recorded webhook deliveries at a running receiver.

The deliveries in `fixtures/webhook-deliveries.json` are shaped like GitHub's and
signed with the configured secret, so this drives the receiver's real path rather
than a test double of it: HMAC verification, delivery de-duplication, the bot
sender filter, and the debounce window that turns a burst of labels into one
scout session.

It asserts rather than demonstrates, because a demo that cannot fail is not
evidence: every delivery is sent twice and the second must come back
`duplicate`, and a body tampered with after signing must come back 401.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DELIVERIES_PATH = REPO_ROOT / "fixtures" / "webhook-deliveries.json"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def load_deliveries(path: Path | None = None) -> list[dict[str, Any]]:
    return list(json.loads((path or DELIVERIES_PATH).read_text(encoding="utf-8")))


async def _post(
    client: httpx.AsyncClient,
    target: str,
    secret: str,
    delivery: dict[str, Any],
    *,
    tamper: bool = False,
) -> str:
    body = json.dumps(delivery["payload"]).encode()
    signature = sign(secret, body if not tamper else body + b" ")
    response = await client.post(
        target,
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": delivery["event"],
            "x-github-delivery": delivery["delivery"],
            "x-hub-signature-256": signature,
        },
    )
    if response.status_code == 401:
        return "rejected"
    response.raise_for_status()
    return str(response.json().get("status"))


async def fire(target: str, secret: str, path: Path | None = None) -> dict[str, int]:
    deliveries = load_deliveries(path)
    tally: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for delivery in deliveries:
            first = await _post(client, target, secret, delivery)
            second = await _post(client, target, secret, delivery)
            if first != delivery["expect"]:
                raise SystemExit(
                    f"{delivery['delivery']}: expected {delivery['expect']}, got {first}"
                )
            if second != "duplicate":
                raise SystemExit(f"{delivery['delivery']}: redelivery was not de-duplicated")
            tally[first] = tally.get(first, 0) + 1
            tally["duplicate"] = tally.get("duplicate", 0) + 1
            print(
                f"{delivery['delivery']} {delivery['event']:<14} "
                f"{first}/{second} — {delivery['note']}"
            )

        tampered = await _post(client, target, secret, deliveries[0], tamper=True)
        if tampered != "rejected":
            raise SystemExit("a tampered body was accepted; the HMAC check is not doing anything")
        tally["rejected"] = 1
        print(f"{'tampered':<19} {'issues':<14} rejected — signature over a modified body")
    return tally


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="http://localhost:8000/webhook/github")
    parser.add_argument("--deliveries", type=Path, default=DELIVERIES_PATH)
    args = parser.parse_args(argv)

    tally = asyncio.run(fire(args.target, settings.github_webhook_secret, args.deliveries))
    print(
        f"\n{sum(tally.values())} deliveries: {tally}\n"
        f"The labelled issues collapse into one scout session — the receiver holds them for "
        f"{settings.batch_window_seconds}s before dispatching."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
