"""Devin resources as code.

Playbooks and knowledge notes live in a Devin account, not in a repo. Defining
them as YAML here and pushing them via the API is what keeps them reviewable:
agent instructions go through PR review like any other code, and a change to a
prompt shows up in `git log` rather than in someone's browser history.

The YAML is also read at runtime — the structured-output schema and ACU ceiling
for each role come from the same file the playbook body came from, so they
cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOURCE_ROOT = REPO_ROOT / "devin"
PLAYBOOK_DIR = RESOURCE_ROOT / "playbooks"
KNOWLEDGE_DIR = RESOURCE_ROOT / "knowledge"
AUTOMATION_DIR = RESOURCE_ROOT / "automations"
LABELS_PATH = REPO_ROOT / "seed" / "labels.yaml"


@dataclass(frozen=True)
class PlaybookSpec:
    key: str
    title: str
    body: str
    macro: str | None
    role: str
    tier: str | None
    max_acu_limit: float | None
    structured_output_schema: dict[str, Any] | None
    structured_output_required: bool
    max_attempts: int | None
    path: Path

    @property
    def env_var(self) -> str:
        return f"PLAYBOOK_{self.key.upper()}"


@dataclass(frozen=True)
class KnowledgeSpec:
    key: str
    name: str
    trigger_description: str
    body: str
    pinned_repo: str | None
    path: Path

    @property
    def env_var(self) -> str:
        return f"KNOWLEDGE_{self.key.upper()}"


@dataclass
class ResourceSet:
    playbooks: dict[str, PlaybookSpec] = field(default_factory=dict)
    knowledge: dict[str, KnowledgeSpec] = field(default_factory=dict)

    def for_tier(self, tier: str) -> PlaybookSpec | None:
        for spec in self.playbooks.values():
            if spec.role == "worker" and spec.tier == tier:
                return spec
        return None

    def by_role(self, role: str) -> PlaybookSpec | None:
        for spec in self.playbooks.values():
            if spec.role == role:
                return spec
        return None


def _resolve_body(raw: dict[str, Any], directory: Path) -> str:
    parts: list[str] = [str(raw.get("body", "")).rstrip()]
    for include in raw.get("includes", []) or []:
        included = (directory / include).read_text(encoding="utf-8").rstrip()
        parts.append(included)
    return "\n\n".join(part for part in parts if part) + "\n"


def load_resources(root: Path = RESOURCE_ROOT) -> ResourceSet:
    resources = ResourceSet()
    playbook_dir = root / "playbooks"
    for path in sorted(playbook_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spec = PlaybookSpec(
            key=raw["key"],
            title=raw["title"],
            body=_resolve_body(raw, playbook_dir),
            macro=raw.get("macro"),
            role=raw.get("role", "worker"),
            tier=raw.get("tier"),
            max_acu_limit=raw.get("max_acu_limit"),
            structured_output_schema=raw.get("structured_output_schema"),
            structured_output_required=bool(raw.get("structured_output_required", True)),
            max_attempts=raw.get("max_attempts"),
            path=path,
        )
        resources.playbooks[spec.key] = spec

    for path in sorted((root / "knowledge").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spec_k = KnowledgeSpec(
            key=raw["key"],
            name=raw["name"],
            trigger_description=raw["trigger_description"].strip(),
            body=str(raw.get("body", "")),
            pinned_repo=raw.get("pinned_repo"),
            path=path,
        )
        resources.knowledge[spec_k.key] = spec_k
    return resources


def load_label_definitions(path: Path = LABELS_PATH) -> list[tuple[str, str, str]]:
    """The taxonomy, read from seed/labels.yaml.

    The labels *are* the state machine, so they get one definition, not two: the
    seeder that creates them and the code that reads them come from the same
    file. `orchestrator.labels` still owns the transitions and the enums — this
    only supplies colours and descriptions.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    definitions: list[tuple[str, str, str]] = []
    for group in ("pipeline", "tier", "escalation"):
        for entry in raw.get(group, []) or []:
            definitions.append(
                (str(entry["name"]), str(entry["color"]), str(entry.get("description", "")))
            )
    return definitions
