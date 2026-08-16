"""Wire and projection models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

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
    #: When the bug was first reported upstream, from the import footer in the
    #: body. `created_at` on this fork is when it was copied in, which is a fact
    #: about the demo rather than about the backlog.
    filed_at: str = ""
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
    def repo(self) -> str | None:
        """Which deployment's repository this session belongs to, as `owner-name`.

        `None` for sessions created before the tag existed, which is why the
        check elsewhere is "mismatch" rather than "not mine".
        """
        for tag in self.tags:
            if tag.startswith("repo:"):
                return tag.split(":", 1)[1]
        return None

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
    #: What each session that worked this issue consumed, keyed by session id.
    #: Kept per session rather than as a running total because the same session
    #: is polled repeatedly and reports a cumulative figure, and because it
    #: lives in the issue's metadata comment: spend survives the session being
    #: archived, which is the only place ACUs are otherwise recoverable from.
    session_acus: dict[str, float] = Field(default_factory=dict)
    #: When the PR appeared, as GitHub reports it. Read from the PR rather than
    #: recorded at dispatch so the figure survives a restart, a lost session, or
    #: a PR the orchestrator only found later.
    pr_opened_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _adopt_legacy_total(cls, data: Any) -> Any:
        """Metadata comments written before the per-session breakdown carry a
        single `acus` total. Keep it rather than reset the issue's spend to
        zero; a later poll of a live session replaces its share."""
        if isinstance(data, dict) and not data.get("session_acus") and data.get("acus"):
            data = {**data, "session_acus": {"legacy": float(data["acus"])}}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def acus(self) -> float:
        return sum(self.session_acus.values())

    def record_spend(self, session_id: str, acus: float) -> None:
        self.session_acus[session_id] = max(self.session_acus.get(session_id, 0.0), acus)


class IssueCard(BaseModel):
    """What the frontend renders. Derived, never authoritative."""

    number: int
    title: str
    html_url: str
    created_at: str = ""
    filed_at: str = ""
    state: State | None = None
    tier: Tier | None = None
    labels: list[str] = Field(default_factory=list)
    meta: IssueMeta = Field(default_factory=IssueMeta)
    session: SessionInfo | None = None
    checks: list[CheckRun] = Field(default_factory=list)
    #: What the worker last said it was doing, straight from its progress
    #: comment. Narrative only — the label is still the state, and this is a
    #: claim about work in flight rather than a verdict about its result.
    progress_phase: str | None = None
    progress_message: str | None = None
    progress_at: str | None = None
    progress_comment_id: int | None = None
    pr_number: int | None = None
    pr_merged: bool = False
    ready_to_merge: bool = False
    last_synced: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pickup_status(self) -> str | None:
        """Triaged, and waiting for a worker that has not started.

        Derived, and deliberately not a label: it says something about this
        process's capacity rather than about the issue, so writing it to GitHub
        would put an operating detail into the durable record. `devin-eligible`
        with no live session is the whole definition.
        """
        if self.state is not State.DEVIN_ELIGIBLE:
            return None
        if self.session is not None and not self.session.terminal:
            return None
        return "awaiting-devin"


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
