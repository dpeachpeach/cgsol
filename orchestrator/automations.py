"""`make automations` — render the committed Automation definitions.

Automations are configured in the Devin UI, which means the thing that decides
what an agent does would otherwise live somewhere with no diff and no review.
Keeping them as YAML here and rendering them for paste-in is the same trade the
playbooks make: the definition is reviewable even when the runtime is not.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from orchestrator.config import get_settings
from orchestrator.resources import AUTOMATION_DIR


def load_automations(directory: Path = AUTOMATION_DIR) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw["path"] = str(path)
        out.append(raw)
    return out


def render(automation: dict[str, Any], repo: str) -> str:
    def substitute(value: str) -> str:
        return value.replace("${GITHUB_REPO}", repo)

    trigger = yaml.safe_dump(automation.get("trigger", {}), sort_keys=False).strip()
    lines = [
        f"# {automation['name']}  ({automation['path']})",
        f"enabled: {automation.get('enabled', True)}",
        f"playbook: {automation.get('playbook') or '(none)'}",
        f"max_acu_limit: {automation.get('max_acu_limit')}",
        f"tags: {', '.join(automation.get('tags', []))}",
        "",
        "--- trigger ---",
        substitute(trigger),
        "",
        "--- prompt ---",
        substitute(str(automation.get("prompt", "")).rstrip()),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Devin Automation definitions")
    parser.add_argument("--key", help="render only this automation")
    args = parser.parse_args(argv)

    repo = get_settings().github_repo
    automations = [a for a in load_automations() if not args.key or a.get("key") == args.key]
    if not automations:
        print("no automation definitions found", file=sys.stderr)
        return 1
    print("\n\n".join(render(a, repo) for a in automations))
    print(
        "\n\nConfigure these at Settings -> Automations. "
        "Triage and worker dispatch are deliberately not here: they have a "
        "branch point, so they live in the orchestrator."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
