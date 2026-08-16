"""`make simulate` — fire webhook deliveries at a running receiver.

Signed with the real secret and shaped like GitHub's payloads, so this exercises
the receiver's actual path: HMAC check, delivery de-duplication, sender filter,
debounce window. Sending unsigned payloads would test nothing worth testing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import get_settings
from orchestrator.labels import State

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "fixtures" / "source-issues.json"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_event(issue: dict[str, Any], repo: str, sender: str) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    return {
        "action": "labeled",
        "label": {"name": State.NEEDS_TRIAGE.value},
        "issue": {
            "number": issue["number"],
            "title": issue["title"],
            "body": issue.get("body", ""),
            "html_url": issue.get("html_url", ""),
            "state": issue.get("state", "open"),
            "labels": [{"name": State.NEEDS_TRIAGE.value}],
            "updated_at": issue.get("updated_at", ""),
        },
        "repository": {"full_name": repo, "name": name, "owner": {"login": owner}},
        "sender": {"login": sender},
    }


async def fire(target: str, secret: str, repo: str, sender: str, limit: int, replay: int) -> int:
    issues = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))[:limit]
    sent = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for issue in issues:
            body = json.dumps(issue_event(issue, repo, sender)).encode()
            delivery = str(uuid.uuid4())
            # Send each delivery `replay` times with the same delivery id: the
            # receiver must accept one and shrug at the rest.
            for attempt in range(replay):
                response = await client.post(
                    target,
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-github-event": "issues",
                        "x-github-delivery": delivery,
                        "x-hub-signature-256": sign(secret, body),
                    },
                )
                status = response.json().get("status")
                print(f"#{issue['number']:>4} attempt {attempt + 1} → {status}")
                sent += 1
    return sent


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="http://localhost:8000/webhook/github")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--replay", type=int, default=2, help="deliveries per event (dedup test)")
    parser.add_argument("--sender", default="dpeachpeach")
    args = parser.parse_args(argv)

    sent = asyncio.run(
        fire(
            args.target,
            settings.github_webhook_secret,
            settings.github_repo,
            args.sender,
            args.limit,
            args.replay,
        )
    )
    print(f"{sent} deliveries sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
