"""Dispatch and the state machine's write side.

Two rules run through everything here:

1. **Idempotent on target state, not on the event.** Act on "this issue is
   currently X and has no live session", never on "this issue was just labelled
   X". Three actors write labels concurrently — a human, Devin, and this server —
   so events arrive duplicated, out of order, and sometimes not at all.
2. **The decision layer is deterministic where it can be.** Filtering, budget
   ceilings, concurrency caps and attempt limits are plain Python. Intelligence
   is reserved for the part that needs judgement, which is the verdict itself.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from orchestrator.config import Settings
from orchestrator.devin import DevinClient
from orchestrator.github import GitHubClient
from orchestrator.labels import (
    RETIREMENT_REASONS,
    EscalationReason,
    State,
    Tier,
    can_transition,
)
from orchestrator.metrics import PIPELINE_TAG, MetricsRegistry
from orchestrator.models import CheckRun, Issue, IssueCard, TriageEstimate, Verdict
from orchestrator.prompts import ci_autofix_prompt, scout_prompt, worker_prompt
from orchestrator.resources import ResourceSet
from orchestrator.state import Store

log = logging.getLogger("cgsol.dispatch")

ACU_PER_ISSUE_TRIAGE = 0.15  # observed scout burn per issue, used for the estimate


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        github: GitHubClient,
        devin: DevinClient,
        store: Store,
        resources: ResourceSet,
        metrics: MetricsRegistry,
    ) -> None:
        self.settings = settings
        self.github = github
        self.devin = devin
        self.store = store
        self.resources = resources
        self.metrics = metrics

    # --- tags -----------------------------------------------------------------

    def _tags(
        self, role: str, issue_number: int | None = None, tier: str | None = None
    ) -> list[str]:
        tags = [self.settings.tag_namespace, PIPELINE_TAG, f"role:{role}"]
        tags.append(f"repo:{self.settings.repo_owner}-{self.settings.repo_name}")
        if issue_number is not None:
            tags.append(f"issue:{issue_number}")
        if tier:
            tags.append(f"tier:{tier}")
        return tags

    def _playbook_id(self, key: str) -> str | None:
        """Playbook IDs come from `make bootstrap`, keyed by the YAML's `key`."""
        ids = {
            "triage_scout": self.settings.playbook_triage_scout,
            "remediate_trivial": self.settings.playbook_remediate_trivial,
            "remediate_medium": self.settings.playbook_remediate_medium,
            "remediate_hard": self.settings.playbook_remediate_hard,
            "ci_autofix": self.settings.playbook_ci_autofix,
        }
        return ids.get(key) or None

    def _knowledge_ids(self) -> list[str]:
        return [value for value in [self.settings.knowledge_superset_conventions] if value]

    # --- triage ---------------------------------------------------------------

    def pre_filter(self, issues: list[Issue]) -> list[Issue]:
        """Deterministic. Plain Python, not intelligence.

        Anything decidable by looking at the issue costs zero ACUs to decide.
        """
        seen_titles: set[str] = set()
        keep: list[Issue] = []
        for issue in issues:
            if issue.state != "open":
                continue
            if issue.has_linked_pr:
                continue
            labelled = any(label in {state.value for state in State} for label in issue.labels)
            if labelled and State.NEEDS_TRIAGE.value not in issue.labels:
                continue
            key = re.sub(r"\W+", " ", issue.title.lower()).strip()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            keep.append(issue)
        return keep[: self.settings.scout_batch_max]

    def estimate(self, issues: list[Issue]) -> TriageEstimate:
        eligible = self.pre_filter(issues)
        return TriageEstimate(
            issue_count=len(eligible),
            estimated_acu=round(
                min(
                    self.settings.acu_ceiling_scout,
                    max(0.5, len(eligible) * ACU_PER_ISSUE_TRIAGE),
                ),
                2,
            ),
            issues=[issue.number for issue in eligible],
        )

    async def triage(self, issues: list[Issue]) -> str | None:
        """One scout session for the whole batch — 20 webhooks, one session."""
        batch = self.pre_filter(issues)
        if not batch:
            return None
        if self.store.session_for_issue(batch[0].number, "scout") is not None:
            log.info("scout already running for this batch, skipping")
            return None
        spec = self.resources.playbooks.get("triage_scout")
        if spec is None:
            raise RuntimeError("triage_scout playbook missing from devin/playbooks")
        if self.settings.dry_run:
            log.info("dry-run: would triage %s", [i.number for i in batch])
            return None

        tags = self._tags("scout")
        tags += [f"issue:{issue.number}" for issue in batch]
        session = await self.devin.create_session(
            scout_prompt(self.settings.github_repo, batch),
            tags=tags,
            title=f"Triage {len(batch)} issues — {self.settings.github_repo}",
            playbook_id=self._playbook_id("triage_scout"),
            knowledge_ids=self._knowledge_ids(),
            max_acu_limit=spec.max_acu_limit,
            structured_output_schema=spec.structured_output_schema,
            structured_output_required=spec.structured_output_required,
        )
        self.store.upsert_session(session)
        self.metrics.record_dispatch("scout")
        await self.store.publish(
            "scout.dispatched",
            {"session_id": session.session_id, "issues": [i.number for i in batch]},
        )
        return session.session_id

    async def apply_verdicts(self, verdicts: list[Verdict]) -> None:
        for verdict in verdicts:
            await self.apply_verdict(verdict)

    async def apply_verdict(self, verdict: Verdict) -> None:
        card = self.store.card(verdict.issue_number)
        if card is None:
            return
        if card.state not in (None, State.NEEDS_TRIAGE):
            return  # someone already moved it; the verdict is stale

        card.meta.confidence = verdict.confidence
        card.meta.scout_reasoning = verdict.reasoning
        card.meta.suggested_approach = verdict.suggested_approach

        tier = verdict.tier_enum
        if not verdict.eligible or tier is None:
            reason = verdict.decline_reason if verdict.decline_reason != "none" else "out-of-scope"
            if reason in RETIREMENT_REASONS and verdict.reasoning.strip():
                # "Already fixed" without evidence is worse than no verdict: it
                # invites a human to close a live bug on an agent's say-so.
                await self._move(card, State.CAN_CLOSE_ISSUE)
                await self.github.upsert_meta(
                    card.number,
                    card.meta,
                    f"**Can close** — `{reason}` (confidence {verdict.confidence:.2f}). "
                    "No code change appears to be needed; a human decides whether to "
                    f"close.\n\n_Evidence:_ {verdict.reasoning}",
                )
                self.metrics.record_escalation(f"can-close-issue:{reason}")
                return
            await self._move(card, State.DEVIN_DECLINED)
            await self.github.upsert_meta(
                card.number,
                card.meta,
                f"**Declined by triage** — `{reason}` (confidence {verdict.confidence:.2f})\n\n"
                f"{verdict.reasoning}",
            )
            self.metrics.record_escalation(f"declined:{reason}")
        elif verdict.confidence < self.settings.confidence_threshold:
            card.meta.escalation = EscalationReason.LOW_CONFIDENCE.value
            await self._move(card, State.HUMAN_REVIEW)
            await self.github.add_labels(
                card.number, [tier.label, EscalationReason.LOW_CONFIDENCE.label]
            )
            await self.github.upsert_meta(
                card.number,
                card.meta,
                f"**Below confidence threshold** ({verdict.confidence:.2f} < "
                f"{self.settings.confidence_threshold}). Routed to a human.\n\n{verdict.reasoning}",
            )
            self.metrics.record_escalation(EscalationReason.LOW_CONFIDENCE.value)
        else:
            card.meta.tier = tier.value
            await self.github.add_labels(card.number, [tier.label])
            await self._move(card, State.DEVIN_ELIGIBLE)
            await self.github.upsert_meta(
                card.number,
                card.meta,
                f"**Eligible** — tier `{tier.value}`, confidence {verdict.confidence:.2f}, "
                f"blast radius `{verdict.blast_radius}`.\n\n"
                f"_Approach:_ {verdict.suggested_approach}\n\n_Reasoning:_ {verdict.reasoning}",
            )

    # --- workers --------------------------------------------------------------

    async def dispatch_ready(self) -> int:
        """Start workers for eligible issues, up to the concurrency cap."""
        dispatched = 0
        capacity = self.settings.max_concurrent_workers - self.store.active_worker_count()
        for card in self.store.by_state(State.DEVIN_ELIGIBLE):
            if capacity <= 0:
                break
            if self.store.session_for_issue(card.number, "worker") is not None:
                continue
            if await self.dispatch_worker(card):
                dispatched += 1
                capacity -= 1
        return dispatched

    async def dispatch_worker(self, card: IssueCard) -> bool:
        tier = card.tier or Tier.MEDIUM
        spec = self.resources.for_tier(tier.value)
        if spec is None:
            log.error("no worker playbook for tier %s", tier.value)
            return False
        if self.settings.dry_run:
            log.info("dry-run: would dispatch worker for #%s", card.number)
            return False

        issue = await self.github.get_issue(card.number)
        session = await self.devin.create_session(
            worker_prompt(self.settings.github_repo, card, issue),
            tags=self._tags("worker", card.number, tier.value),
            title=f"#{card.number} {issue.title[:60]}",
            playbook_id=self._playbook_id(spec.key),
            knowledge_ids=self._knowledge_ids(),
            max_acu_limit=spec.max_acu_limit,
            structured_output_schema=spec.structured_output_schema,
            structured_output_required=spec.structured_output_required,
        )
        card.meta.session_id = session.session_id
        card.meta.attempt += 1
        card.meta.tier = tier.value
        self.store.upsert_session(session)
        await self._move(card, State.DEVIN_WORKING)
        await self.github.upsert_meta(
            card.number,
            card.meta,
            f"**Dispatched** — {tier.value} worker, ceiling {spec.max_acu_limit} ACU.\n"
            f"Session: {session.url or session.session_id}",
        )
        self.metrics.record_dispatch("worker")
        await self.store.publish(
            "worker.dispatched", {"issue": card.number, "session_id": session.session_id}
        )
        return True

    async def on_worker_finished(self, card: IssueCard, output: dict[str, Any] | None) -> None:
        session = card.session
        outcome = (output or {}).get("outcome")
        pr_url = (output or {}).get("pr_url") or (session.pr_url if session else None)

        if outcome == "escalated" or (outcome is None and not pr_url):
            reason = (output or {}).get("escalation_reason") or (
                EscalationReason.SESSION_ERROR.value
            )
            await self.escalate(card, reason, State.DEVIN_BLOCKED)
            return
        if outcome == "no-change-needed":
            await self._move(card, State.HUMAN_REVIEW)
            await self.github.upsert_meta(
                card.number, card.meta, "**Worker reported no change needed.** Human to confirm."
            )
            return

        card.meta.pr_url = pr_url
        card.meta.branch = (output or {}).get("branch")
        await self._move(card, State.DEVIN_PR_OPEN)
        await self.github.upsert_meta(
            card.number,
            card.meta,
            f"**PR opened** — {pr_url}\n\n{(output or {}).get('summary', '')}",
        )

    # --- CI loop --------------------------------------------------------------

    async def evaluate_ci(self, card: IssueCard, checks: list[CheckRun], merged: bool) -> None:
        """The verification loop. CI is the gate; this only reacts to it."""
        if merged:
            await self._move(card, State.DONE)
            return
        if not checks:
            return
        pending = [c for c in checks if c.status != "completed"]
        failing = [c for c in checks if c.conclusion in {"failure", "timed_out", "startup_failure"}]
        if pending and not failing:
            return
        if not failing:
            if card.state in (State.DEVIN_PR_OPEN, State.CI_FAILING, State.DEVIN_FIXING):
                await self._move(card, State.HUMAN_REVIEW)
                await self.github.upsert_meta(
                    card.number,
                    card.meta,
                    f"**CI green** after {card.meta.ci_rounds} autofix round(s). "
                    "Waiting on human review.",
                )
            return

        if self.store.session_for_issue(card.number, "ci-fix") is not None:
            return
        if card.meta.ci_rounds >= self.settings.max_ci_rounds:
            await self.escalate(card, EscalationReason.CI_UNFIXABLE.value, State.HUMAN_REVIEW)
            return
        if card.state is State.DEVIN_PR_OPEN:
            await self._move(card, State.CI_FAILING)
        await self.dispatch_ci_fix(card, failing)

    async def dispatch_ci_fix(self, card: IssueCard, failing: list[CheckRun]) -> bool:
        spec = self.resources.playbooks.get("ci_autofix")
        if spec is None or self.settings.dry_run:
            return False
        round_no = card.meta.ci_rounds + 1
        session = await self.devin.create_session(
            ci_autofix_prompt(self.settings.github_repo, card, failing, round_no),
            tags=self._tags("ci-fix", card.number, card.meta.tier),
            title=f"CI autofix #{card.number} (round {round_no})",
            playbook_id=self._playbook_id("ci_autofix"),
            knowledge_ids=self._knowledge_ids(),
            max_acu_limit=spec.max_acu_limit,
            structured_output_schema=spec.structured_output_schema,
            structured_output_required=spec.structured_output_required,
        )
        card.meta.ci_rounds = round_no
        card.meta.session_id = session.session_id
        self.store.upsert_session(session)
        await self._move(card, State.DEVIN_FIXING)
        await self.github.upsert_meta(
            card.number,
            card.meta,
            f"**CI autofix round {round_no}/{self.settings.max_ci_rounds}** — "
            f"{', '.join(check.name for check in failing)}\nSession: {session.url}",
        )
        self.metrics.record_dispatch("ci-fix")
        return True

    async def on_ci_fix_finished(self, card: IssueCard, output: dict[str, Any] | None) -> None:
        outcome = (output or {}).get("outcome")
        if outcome == "pushed-fix":
            await self._move(card, State.DEVIN_PR_OPEN)  # back to waiting on the gate
            return
        reason = (output or {}).get("escalation_reason") or EscalationReason.CI_UNFIXABLE.value
        await self.escalate(card, reason, State.HUMAN_REVIEW)

    # --- helpers --------------------------------------------------------------

    async def _move(self, card: IssueCard, target: State) -> None:
        if card.state is target:
            return
        if not can_transition(card.state, target):
            log.warning(
                "refusing illegal transition #%s %s -> %s",
                card.number,
                card.state,
                target,
            )
            return
        await self.github.set_state(card.number, target, card.labels)
        previous = card.state
        card.state = target
        card.labels = [label for label in card.labels if label not in {s.value for s in State}] + [
            target.value
        ]
        card.last_synced = time.time()
        await self.store.publish(
            "issue.state",
            {
                "issue": card.number,
                "from": previous.value if previous else None,
                "to": target.value,
            },
        )

    async def escalate(self, card: IssueCard, reason: str, target: State) -> None:
        card.meta.escalation = reason
        self.metrics.record_escalation(reason)
        try:
            label = EscalationReason(reason).label
        except ValueError:
            label = EscalationReason.SESSION_ERROR.label
        await self._move(card, target)
        await self.github.add_labels(card.number, [label])
        await self.github.upsert_meta(
            card.number,
            card.meta,
            f"**Escalated to a human** — `{reason}` after {card.meta.attempt} attempt(s) "
            f"and {card.meta.ci_rounds} CI round(s).",
        )
        await self.store.publish("issue.escalated", {"issue": card.number, "reason": reason})
