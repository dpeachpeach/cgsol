"""`make bootstrap` — push devin/*.yaml into the Devin account.

Playbooks and knowledge notes are account resources, not repo files, so without
this step the prompts that decide what an agent does would live somewhere with
no diff, no review, and no history. Creating them from YAML is what makes
"agent instructions go through PR review like code" true rather than aspirational.

Idempotent: matches existing resources by title/name and updates in place, then
writes the resulting IDs back to .env so the orchestrator can reference them.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import get_settings
from orchestrator.resources import ResourceSet, load_resources

ENV_PATH = Path(".env")


async def _list(client: httpx.AsyncClient, path: str, key: str) -> list[dict[str, Any]]:
    response = await client.get(path)
    if response.status_code != 200:
        return []
    payload = response.json()
    if isinstance(payload, list):
        return payload
    value = payload.get(key, [])
    return value if isinstance(value, list) else []


async def push(resources: ResourceSet, dry_run: bool = False) -> dict[str, str]:
    settings = get_settings()
    if not settings.devin_api_key and not dry_run:
        raise SystemExit("DEVIN_API_KEY is required (or pass --dry-run)")

    ids: dict[str, str] = {}
    async with httpx.AsyncClient(
        base_url=settings.devin_api_base,
        headers={"Authorization": f"Bearer {settings.devin_api_key}"},
        timeout=60.0,
    ) as client:
        existing_playbooks = {
            item.get("title"): item.get("playbook_id")
            for item in await _list(client, "/v1/playbooks", "playbooks")
        }
        for spec in resources.playbooks.values():
            payload = {"title": spec.title, "body": spec.body, "macro": spec.macro}
            playbook_id = existing_playbooks.get(spec.title)
            if dry_run:
                print(f"[dry-run] {'update' if playbook_id else 'create'} playbook {spec.title}")
                continue
            if playbook_id:
                response = await client.put(f"/v1/playbooks/{playbook_id}", json=payload)
                response.raise_for_status()
            else:
                response = await client.post("/v1/playbooks", json=payload)
                response.raise_for_status()
                playbook_id = response.json()["playbook_id"]
            print(f"playbook {spec.title} -> {playbook_id}")
            ids[spec.env_var] = str(playbook_id)

        existing_notes = {
            item.get("name"): item.get("id") or item.get("note_id")
            for item in await _list(client, "/v1/knowledge", "knowledge")
        }
        for note in resources.knowledge.values():
            payload = {
                "name": note.name,
                "body": note.body,
                "trigger_description": note.trigger_description,
                "pinned_repo": note.pinned_repo,
            }
            note_id = existing_notes.get(note.name)
            if dry_run:
                print(f"[dry-run] {'update' if note_id else 'create'} knowledge {note.name}")
                continue
            if note_id:
                response = await client.put(f"/v1/knowledge/{note_id}", json=payload)
                response.raise_for_status()
            else:
                response = await client.post("/v1/knowledge", json=payload)
                response.raise_for_status()
                note_id = response.json()["id"]
            print(f"knowledge {note.name} -> {note_id}")
            ids[note.env_var] = str(note_id)
    return ids


def write_env(ids: dict[str, str], path: Path = ENV_PATH) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    keys = set(ids)
    out = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
    out += [f"{key}={value}" for key, value in sorted(ids.items())]
    path.write_text("\n".join(out).strip() + "\n")
    print(f"wrote {len(ids)} ids to {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push Devin resources from devin/*.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=str(ENV_PATH))
    args = parser.parse_args(argv)

    resources = load_resources()
    ids = asyncio.run(push(resources, dry_run=args.dry_run))
    if ids:
        write_env(ids, Path(args.env))
    return 0


if __name__ == "__main__":
    sys.exit(main())
