"""Wire and projection models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from orchestrator.labels import State, Tier

Role = Literal["scout", "worker", "ci-fix", "review", "unknown"]


class Issue(BaseModel):
    number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    state: str = "open"
    html_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    has_linked_pr: bool = False


class Verdict(BaseModel):
    issue_number: int
    eligible: bool
    confidence: float = 0.0
    tier: str = "none"
    blast_radius: str = "unknown"
    suggested_approach: str = ""
    reasoning: str = ""
    decline_reason: str = "none"

    @property
    def tier_enum(self) -> Tier | None:
        try:
            return Tier(self.tier)
        except ValueError:
            return None


class SessionInfo(BaseModel):
    session_id: str
    status: str = "unknown"
    status_enum: str | None = None
    tags: list[str] = Field(default_factory=list)
    title: str | None = None
    url: str = ""
    pr_url: str | None = None
    acus_consumed: float = 0.0
    structured_output: dict[str, Any] | None = None
    origin: str | None = None
    playbook_id: str | None = None
    updated_at: str | None = None

    @property
    def role(self) -> Role:
        for tag in self.tags:
            if tag.startswith("role:"):
                value = tag.split(":", 1)[1]
                if value in ("scout", "worker", "ci-fix", "review"):
                    return value  # type: ignore[return-value]
        return "unknown"

    @property
    def issue_number(self) -> int | None:
        for tag in self.tags:
            if tag.startswith("issue:"):
                try:
                    return int(tag.split(":", 1)[1])
                except ValueError:
                    return None
        return None

    @property
    def issue_numbers(self) -> list[int]:
        """Every issue this session was started for. A scout batch carries one
        tag per issue, and that list is what its output has to cover."""
        numbers: list[int] = []
        for tag in self.tags:
            if tag.startswith("issue:"):
                try:
                    numbers.append(int(tag.split(":", 1)[1]))
                except ValueError:
                    continue
        return numbers

    @property
    def terminal(self) -> bool:
        return self.status in {"exit", "error", "expired"} or self.status_enum in {
            "finished",
            "expired",
        }

    @property
    def waiting(self) -> bool:
        # `blocked` is v1's word for it; v3 says which kind of block it is.
        return self.status_enum in {
            "blocked",
            "waiting_for_user",
            "waiting_for_approval",
        } or self.status in {"suspended"}


class IssueMeta(BaseModel):
    """The per-issue metadata blob, stored in an HTML comment on the issue.

    The session <-> issue link is written on both sides (here, and as session
    tags) so it survives losing either one.
    """

    session_id: str | None = None
    tier: str | None = None
    attempt: int = 0
    ci_rounds: int = 0
    human_turns: int = 0
    confidence: float | None = None
    pr_url: str | None = None
    branch: str | None = None
    escalation: str | None = None
    scout_reasoning: str | None = None
    suggested_approach: str | None = None
    acus: float = 0.0
    #: When the PR appeared, as GitHub reports it. Read from the PR rather than
    #: recorded at dispatch so the figure survives a restart, a lost session, or
    #: a PR the orchestrator only found later.
    pr_opened_at: str | None = None


class IssueCard(BaseModel):
    """What the frontend renders. Derived, never authoritative."""

    number: int
    title: str
    html_url: str
    created_at: str = ""
    state: State | None = None
    tier: Tier | None = None
    labels: list[str] = Field(default_factory=list)
    meta: IssueMeta = Field(default_factory=IssueMeta)
    session: SessionInfo | None = None
    checks: list[CheckRun] = Field(default_factory=list)
    pr_number: int | None = None
    pr_merged: bool = False
    ready_to_merge: bool = False
    last_synced: float = 0.0


class CheckRun(BaseModel):
    name: str
    status: str
    conclusion: str | None = None
    details_url: str | None = None
    required: bool = False


IssueCard.model_rebuild()


class TriageEstimate(BaseModel):
    issue_count: int
    estimated_acu: float
    issues: list[int]
