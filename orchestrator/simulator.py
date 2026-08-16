"""Replay mode: a simulated GitHub and a simulated Devin, behind the same
httpx transport seam the live clients use.

Why not a plain cassette. A recorded cassette answers reads with whatever was
recorded, so it cannot answer *this* run's writes — reconciliation would read
back labels the orchestrator has not written yet and walk cards backwards. The
whole point of the system is that GitHub is the source of truth and the
projection re-derives from it, so replay has to preserve that property or it is
replaying something else.

So replay runs against an in-memory fork seeded from a real snapshot of
`dpeachpeach/superset-cg` (`fixtures/source-issues.json`) plus the seed corpus's
ground truth. Reads reflect writes. The orchestrator code path is identical:
same clients, same reconciler, same state machine, no `if replay:` anywhere
above the transport.

The Devin side is simulated rather than recorded for the same reason — sessions
have to be created by *this* run to correlate with the issues this run triaged —
and its verdicts come from `seed/issues.yaml`'s `disposition`/`assessment`
fields, which are the human answer key. That makes the replayed triage
reproducible and, more usefully, scoreable.

`RECORD=true` still records real traffic to cassettes, and `REPLAY_CASSETTE=true`
plays those back strictly. Replay-by-simulation is the default because it is the
one an evaluator with no credentials can run.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "fixtures" / "source-issues.json"
CORPUS_PATH = REPO_ROOT / "seed" / "issues.yaml"

#: How long a simulated session runs before it finishes. Real sessions take
#: minutes; the replay compresses the timeline so the whole pipeline is visible.
SESSION_SECONDS = 6.0
CI_SECONDS = 5.0

_TIER_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("trivial", ("typo", "locale", "format", "duplicate iso", "annotation", "openapi")),
    ("hard", ("freeze", "pagination", "state", "export", "import", "color")),
]

_TIER_ACUS = {"trivial": 0.9, "medium": 2.1, "hard": 3.8}


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32]


def _json(payload: Any, status: int = 200, request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
        request=request,
    )


class SimulatedWorld:
    """One in-memory fork plus one in-memory Devin org, sharing a clock.

    Both simulators write into this so the story stays coherent: a worker
    session finishing is the same event as a PR appearing on the fork.
    """

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.prs: list[dict[str, Any]] = []
        self.checks: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[str, str] = {}
        self.labels: dict[str, dict[str, str]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.corpus: dict[str, dict[str, Any]] = {}
        self._next_comment_id = 1_000
        self._next_pr = 100
        self._next_session = 1
        self._load()

    # --- seeding --------------------------------------------------------------

    def _load(self) -> None:
        if SNAPSHOT_PATH.exists():
            for raw in json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")):
                self.issues[raw["number"]] = {
                    "number": raw["number"],
                    "title": raw["title"],
                    "body": raw.get("body", ""),
                    "html_url": raw["html_url"],
                    "state": raw.get("state", "open"),
                    # Replay starts where the real fork starts: unlabelled.
                    # Everything the dashboard shows is produced by this run.
                    "labels": [],
                    "updated_at": raw.get("updated_at", ""),
                }
        if CORPUS_PATH.exists():
            corpus = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8")) or {}
            for entry in corpus.get("issues", []):
                self.corpus[entry["title"]] = entry

    # --- ground truth ---------------------------------------------------------

    def verdict_for(self, number: int) -> dict[str, Any]:
        """The scout's answer, derived from the corpus's human answer key."""
        issue = self.issues[number]
        entry = self.corpus.get(issue["title"], {})
        disposition = entry.get("disposition", "eligible")
        assessment = entry.get("assessment", "plausible")
        title = issue["title"].lower()

        if disposition == "decline":
            reason = entry.get("decline_reason") or "product-semantics"
            return {
                "issue_number": number,
                "eligible": False,
                "confidence": 0.9,
                "tier": "none",
                "blast_radius": "unknown",
                "suggested_approach": "none",
                "decline_reason": reason,
                "reasoning": (
                    f"Needs a decision a maintainer has to make ({reason}). The issue does "
                    "not say what correct behaviour is, so any patch would be inventing it."
                ),
            }

        tier = "medium"
        for candidate, keywords in _TIER_KEYWORDS:
            if any(keyword in title for keyword in keywords):
                tier = candidate
                break
        confidence = 0.87 if assessment == "confident" else 0.58
        return {
            "issue_number": number,
            "eligible": True,
            "confidence": confidence,
            "tier": tier,
            "blast_radius": "single module" if tier != "hard" else "multiple modules",
            "suggested_approach": (
                "Reproduce from the described steps, fix at the narrowest layer that owns "
                "the behaviour, and let the repo's own CI verify."
            ),
            "reasoning": (
                "Self-contained and testable from the description; the acceptance criterion "
                "is stated in the issue."
                if assessment == "confident"
                else "Plausible but the expected behaviour is only implied, so confidence is "
                "below the dispatch threshold."
            ),
        }

    # --- session lifecycle ----------------------------------------------------

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._next_session += 1
        session_id = f"devin-sim-{self._next_session:04d}"
        tags = list(payload.get("tags") or [])
        session = {
            "session_id": session_id,
            "status": "running",
            "status_enum": "working",
            "tags": tags,
            "title": payload.get("title"),
            "url": f"https://app.devin.ai/sessions/{session_id}",
            "created_at": time.time(),
            "acus_consumed": 0.0,
            "structured_output": None,
            "origin": "api",
            "playbook_id": payload.get("playbook_id"),
            "pull_requests": [],
        }
        self.sessions[session_id] = session
        return session

    def _role(self, session: dict[str, Any]) -> str:
        for tag in session["tags"]:
            if tag.startswith("role:"):
                return tag.split(":", 1)[1]
        return "worker"

    def _issue_numbers(self, session: dict[str, Any]) -> list[int]:
        return [int(tag.split(":", 1)[1]) for tag in session["tags"] if tag.startswith("issue:")]

    def advance(self) -> None:
        """Move any session whose simulated runtime has elapsed to finished."""
        now = time.time()
        for session in self.sessions.values():
            if session["status"] == "finished":
                continue
            age = now - session["created_at"]
            role = self._role(session)
            duration = CI_SECONDS if role == "ci-fix" else SESSION_SECONDS
            if age < duration:
                session["acus_consumed"] = round(min(age / duration, 1.0) * 1.5, 2)
                continue
            self._finish(session, role)

    def _finish(self, session: dict[str, Any], role: str) -> None:
        session["status"] = "finished"
        session["status_enum"] = "finished"
        numbers = self._issue_numbers(session)
        if role == "scout":
            session["acus_consumed"] = round(0.14 * max(1, len(numbers)), 2)
            session["structured_output"] = {
                "verdicts": [
                    self.verdict_for(number) for number in numbers if number in self.issues
                ]
            }
        elif role == "worker":
            number = numbers[0]
            tier = next(
                (t.split(":", 1)[1] for t in session["tags"] if t.startswith("tier:")), "medium"
            )
            session["acus_consumed"] = _TIER_ACUS.get(tier, 2.0)
            pr = self._open_pr(number)
            session["pull_requests"] = [{"pr_url": pr["html_url"]}]
            session["structured_output"] = {
                "outcome": "pr-opened",
                "issue_number": number,
                "pr_url": pr["html_url"],
                "branch": pr["head"]["ref"],
                "files_changed": 2 if tier != "hard" else 5,
                "summary": "Minimal fix at the layer that owns the behaviour. CI verifies.",
            }
        elif role == "ci-fix":
            number = numbers[0]
            session["acus_consumed"] = 0.8
            fixed = self._resolve_ci(number)
            session["structured_output"] = (
                {
                    "outcome": "pushed-fix",
                    "issue_number": number,
                    "summary": "Corrected the lint failure the check reported.",
                }
                if fixed
                else {
                    "outcome": "escalated",
                    "issue_number": number,
                    "escalation_reason": "ci-unfixable",
                    "summary": "The failure is environmental, not in the diff.",
                }
            )

    # --- fork mutation --------------------------------------------------------

    def _open_pr(self, number: int) -> dict[str, Any]:
        self._next_pr += 1
        issue = self.issues[number]
        sha = f"sha{self._next_pr:06d}"
        pr = {
            "number": self._next_pr,
            "title": issue["title"],
            "body": f"Closes #{number}\n\nAutomated fix.",
            "html_url": f"https://github.com/dpeachpeach/superset-cg/pull/{self._next_pr}",
            "state": "open",
            "merged_at": None,
            "head": {"ref": f"devin/issue-{number}-{_slug(issue['title'])}", "sha": sha},
        }
        self.prs.append(pr)
        # Roughly a third of first attempts come back red — that is the point of
        # having a CI loop at all, so the replay has to show it.
        red = number % 3 == 0
        self.checks[sha] = [
            {
                "name": "pre-commit",
                "status": "completed",
                "conclusion": "failure" if red else "success",
            },
            {"name": "frontend-build", "status": "completed", "conclusion": "success"},
            {"name": "python-unit", "status": "completed", "conclusion": "success"},
        ]
        return pr

    def _resolve_ci(self, number: int) -> bool:
        pr = next((p for p in reversed(self.prs) if f"issue-{number}-" in p["head"]["ref"]), None)
        if pr is None:
            return False
        # One issue stays red on purpose, so the round limit and the escalation
        # path are visible in the replay rather than merely implemented.
        if number % 9 == 0:
            return False
        self.checks[pr["head"]["sha"]] = [
            {"name": check["name"], "status": "completed", "conclusion": "success"}
            for check in self.checks.get(pr["head"]["sha"], [])
        ]
        return True

    # --- comments / labels ----------------------------------------------------

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        self._next_comment_id += 1
        comment = {"id": self._next_comment_id, "body": body, "user": {"login": "cgsol[bot]"}}
        self.comments.setdefault(number, []).append(comment)
        return comment

    def update_comment(self, comment_id: int, body: str) -> None:
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return


_world: SimulatedWorld | None = None


def world() -> SimulatedWorld:
    global _world
    if _world is None:
        _world = SimulatedWorld()
    return _world


def reset_world() -> None:
    global _world
    _world = None


class SimulatedGitHubTransport(httpx.AsyncBaseTransport):
    """Enough of the GitHub REST API for the orchestrator to be itself."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        state = world()
        path = request.url.path
        method = request.method.upper()
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues", path):
            del match
            if method == "GET":
                page = int(request.url.params.get("page", 1))
                if page > 1:
                    return _json([], request=request)
                wanted = request.url.params.get("state", "open")
                issues = [
                    issue
                    for issue in state.issues.values()
                    if wanted == "all" or issue["state"] == wanted
                ]
                return _json([_issue_json(issue) for issue in issues], request=request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path):
            number = int(match.group(1))
            issue = state.issues.get(number)
            if issue is None:
                return _json({"message": "Not Found"}, 404, request)
            return _json(_issue_json(issue), request=request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)/comments", path):
            number = int(match.group(1))
            if method == "GET":
                return _json(state.comments.get(number, []), request=request)
            comment = state.add_comment(number, body.get("body", ""))
            return _json(comment, 201, request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)", path):
            state.update_comment(int(match.group(1)), body.get("body", ""))
            return _json({"id": int(match.group(1))}, request=request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)/labels", path):
            number = int(match.group(1))
            issue = state.issues[number]
            for label in body.get("labels", []):
                if label not in issue["labels"]:
                    issue["labels"].append(label)
            return _json([{"name": name} for name in issue["labels"]], request=request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)/labels/(.+)", path):
            number, label = int(match.group(1)), match.group(2)
            issue = state.issues[number]
            issue["labels"] = [name for name in issue["labels"] if name != label]
            return _json([{"name": name} for name in issue["labels"]], request=request)

        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls", path):
            return _json(state.prs, request=request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/commits/([^/]+)/check-runs", path):
            runs = state.checks.get(match.group(1), [])
            return _json({"check_runs": runs}, request=request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/contents/(.+)", path):
            file_path = match.group(1)
            if method == "GET":
                content = state.files.get(file_path)
                if content is None:
                    return _json({"message": "Not Found"}, 404, request)
                return httpx.Response(200, text=content, request=request)
            state.files[file_path] = _decode_content(body.get("content", ""))
            return _json({"content": {"path": file_path}}, request=request)

        if re.fullmatch(r"/repos/[^/]+/[^/]+/labels", path):
            name = body.get("name", "")
            state.labels[name] = body
            return _json(body, 201, request)

        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/labels/(.+)", path):
            return _json({"name": match.group(1)}, request=request)

        return _json({"message": f"simulator: unhandled {method} {path}"}, 404, request)


class SimulatedDevinTransport(httpx.AsyncBaseTransport):
    """Enough of the Devin API to run the pipeline: create, list by tag, detail."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        state = world()
        state.advance()
        path = request.url.path
        method = request.method.upper()
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}

        if method == "POST" and re.fullmatch(r"/v3/organizations/[^/]+/sessions", path):
            return _json(_session_json(state.create_session(body)), 201, request)

        if path == "/v1/sessions":
            wanted = set(request.url.params.get_list("tags"))
            sessions = [
                _session_json(session)
                for session in state.sessions.values()
                if wanted.issubset(set(session["tags"]))
            ]
            return _json({"sessions": sessions}, request=request)

        if match := re.fullmatch(r"/v3/organizations/[^/]+/sessions/(.+)", path):
            session = state.sessions.get(match.group(1))
            if session is None:
                return _json({"message": "Not Found"}, 404, request)
            return _json(_session_json(session), request=request)

        if match := re.fullmatch(r"/v1/session/([^/]+)", path):
            session = state.sessions.get(match.group(1))
            if session is None:
                return _json({"message": "Not Found"}, 404, request)
            return _json(_session_json(session), request=request)

        if re.fullmatch(r"/v1/session/[^/]+/message", path):
            return _json({"ok": True}, request=request)

        return _json({"message": f"simulator: unhandled {method} {path}"}, 404, request)


def _issue_json(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "labels": [{"name": name} for name in issue["labels"]],
        "state": issue["state"],
        "html_url": issue["html_url"],
        "updated_at": issue["updated_at"],
    }


def _session_json(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "status_enum": session["status_enum"],
        "tags": session["tags"],
        "title": session["title"],
        "url": session["url"],
        "acus_consumed": session["acus_consumed"],
        "structured_output": session["structured_output"],
        "origin": session["origin"],
        "playbook_id": session["playbook_id"],
        "pull_requests": session["pull_requests"],
        "updated_at": "",
    }


def _decode_content(encoded: str) -> str:
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except ValueError:
        return encoded
