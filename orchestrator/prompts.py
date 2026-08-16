"""Prompt construction. Playbooks carry the standing instructions; these carry
only the per-session facts, so a prompt change is never a policy change."""

from __future__ import annotations

from orchestrator.models import CheckRun, Issue, IssueCard

MAX_BODY_CHARS = 2500


def _trim(text: str, limit: int = MAX_BODY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def scout_prompt(repo: str, issues: list[Issue]) -> str:
    blocks = []
    for issue in issues:
        blocks.append(
            f"### Issue #{issue.number} — {issue.title}\n"
            f"URL: {issue.html_url}\n"
            f"Labels: {', '.join(issue.labels) or 'none'}\n\n"
            f"{_trim(issue.body) or '_no body_'}"
        )
    body = "\n\n---\n\n".join(blocks)
    return (
        f"Triage {len(issues)} open issues in `{repo}` for agent-solvability.\n\n"
        "You are read-only: no clone, no dependency install, no tests, no writes of "
        "any kind. Assess every issue below and emit one structured output covering "
        "all of them.\n\n"
        f"{body}\n"
    )


def worker_prompt(repo: str, card: IssueCard, issue: Issue) -> str:
    approach = card.meta.suggested_approach or "(none recorded)"
    slug = "-".join(issue.title.lower().split()[:5]).strip("-")
    slug = "".join(ch for ch in slug if ch.isalnum() or ch == "-") or "fix"
    return (
        f"Resolve issue #{issue.number} in `{repo}`.\n\n"
        f"**{issue.title}**\n{issue.html_url}\n\n"
        f"{_trim(issue.body) or '_no body_'}\n\n"
        "---\n\n"
        f"Triage tier: **{card.tier.value if card.tier else 'unknown'}** "
        f"(confidence {card.meta.confidence if card.meta.confidence is not None else '—'}).\n"
        f"Scout's suggested approach: {approach}\n\n"
        f"Branch: `devin/issue-{issue.number}-{slug}`.\n"
        f"PR body must contain `Closes #{issue.number}`.\n\n"
        "Do not set up the development environment and do not run the test suite. "
        "Lint the files you changed, push, open the PR, and stop — CI is the gate, "
        "and a separate session owns any failure it reports."
    )


def ci_autofix_prompt(repo: str, card: IssueCard, failing: list[CheckRun], round_no: int) -> str:
    checks = "\n".join(
        f"- **{check.name}** → {check.conclusion or check.status}"
        f" ({check.details_url or 'no log url'})"
        for check in failing
    )
    return (
        f"CI is red on {card.meta.pr_url} (issue #{card.number} in `{repo}`).\n\n"
        f"Failing checks:\n{checks}\n\n"
        f"This is autofix round {round_no}. Fix **only** what these checks report, on the "
        "PR's existing branch. Read the logs from the details URLs above before editing, "
        "and name the root cause in your structured output.\n\n"
        "Do not weaken the gate: no skipped or deleted tests, no new lint suppressions, "
        "no changes under `.github/workflows/`. If the failure is flaky or infrastructural "
        "rather than caused by this PR, say so and stop — that is a valid answer.\n\n"
        "Do not run the test suite locally. Push the fix; the pipeline re-runs the check."
    )
