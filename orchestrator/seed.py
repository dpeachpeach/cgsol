"""`make seed` — labels first, then issues, into $GITHUB_REPO.

This already ran against the working fork. It is kept, and kept correct, so the
corpus is reproducible: a demo nobody else can stand up is a video, not a system.

Invariants (see seed/README.md), each of which exists because breaking it is
silent rather than loud:

* Labels are created before issues, because issues reference them.
* ``hold_back: true`` is never filed — one issue is reserved to file live.
* Idempotent on title, so a re-run files nothing twice.
* 1–2s between creates: GitHub secondary rate limits answer ``403``, not ``429``,
  so retry-on-429 never sees them.
* No ``tier`` and no pipeline labels are applied. Triage assigns tier; issues
  enter at ``needs-triage`` through the orchestrator. A seeder that pre-assigns
  tiers is scoring its own exam.

Bodies in ``seed/issues.yaml`` are already sanitized (``seed/sanitize.py``);
``--verify`` re-runs the leak assertions before anything is written.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path
from typing import Any

import yaml

from orchestrator.config import get_settings
from orchestrator.github import GitHubClient
from orchestrator.resources import load_label_definitions

SEED_DIR = Path("seed")


def _footer(source: int) -> str:
    """Provenance in plain text. A `#`-ref here would autolink to the wrong issue."""
    return f"\n\n---\n_Imported from apache/superset issue {source} for demonstration._"


def verify(issues: list[dict[str, Any]]) -> None:
    sys.path.insert(0, str(SEED_DIR))
    from sanitize import assert_clean  # noqa: PLC0415  (vendored corpus tool)

    for entry in issues:
        assert_clean(entry.get("body", ""), label=f"issue {entry['source']}")
        assert_clean(entry.get("title", ""), label=f"title {entry['source']}")
    print(f"sanitizer: {len(issues)} bodies clean")


async def seed(path: Path, dry_run: bool) -> int:
    settings = get_settings()
    corpus = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    issues: list[dict[str, Any]] = corpus.get("issues", [])

    github = GitHubClient(settings)
    try:
        if dry_run:
            print(f"[dry-run] would ensure {len(load_label_definitions())} labels")
        else:
            created = await github.ensure_labels()
            print(f"labels ensured ({created} created)")

        existing = {issue.title for issue in await github.list_issues(state="all", limit=100)}
        filed = 0
        for entry in issues:
            title = entry["title"]
            if entry.get("hold_back"):
                print(f"held back for the live demo: {title}")
                continue
            if title in existing:
                print(f"skip (exists): {title}")
                continue
            body = entry.get("body", "").rstrip() + _footer(entry["source"])
            if dry_run:
                print(f"[dry-run] create: {title}")
                continue
            number = await github.create_issue(title, body, labels=[])
            print(f"created #{number}: {title}")
            filed += 1
            await asyncio.sleep(random.uniform(1.0, 2.0))
        return filed
    finally:
        await github.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed labels and issues into $GITHUB_REPO")
    parser.add_argument("--path", default=str(SEED_DIR / "issues.yaml"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="re-run sanitizer leak assertions")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if args.verify:
        verify((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("issues", []))
    filed = asyncio.run(seed(path, args.dry_run))
    print(f"filed {filed} issues into {get_settings().github_repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
