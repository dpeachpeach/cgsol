"""Sanitize apache/superset issue bodies before filing them into the fork.

Three of these rewrites are not cosmetic:
  - @mentions notify real Apache contributors
  - owner/repo#N and issue URLs create backlinks on the SOURCE repo
  - bare #N autolinks to an unrelated issue in the TARGET repo

Verified against the seed corpus: zero surviving mentions, refs, or backlinking URLs.
"""
import re

_RULES = [
    # Full issue/PR/discussion URLs -> plain text (would backlink the source repo)
    (r"https?://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull|discussions)/(\d+)/?", r"\1/\2 item \3"),
    # owner/repo#123 -> plain text
    (r"(?<![\w/])([\w.-]+)/([\w.-]+)#(\d+)\b", r"\1/\2 item \3"),
    # bare #123 -> plain text (autolinks to the WRONG issue in the target repo)
    (r"(?<![\w&#])#(\d+)\b", r"superset issue \1"),
    # @mentions -> plain text (would notify real people). Skips emails and paths.
    (r"(?<![\w`/.=-])@([A-Za-z0-9][\w-]*)", r"\1"),
    # tidy "issue superset issue N" left by the rewrite above
    (r"\bissues?\s+superset issue (\d+)", r"superset issue \1"),
]

# Anything matching these AFTER sanitizing is a leak. Assert on it.
LEAK_PATTERNS = {
    "@mention":  r"(?<![\w`/.=-])@[A-Za-z0-9]",
    "bare #ref": r"(?<![\w&#])#\d+\b",
    "repo#ref":  r"(?<![\w/])[\w.-]+/[\w.-]+#\d+\b",
    "issue URL": r"github\.com/[\w.-]+/[\w.-]+/(?:issues|pull|discussions)/\d+",
}


def sanitize(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n")
    for pattern, repl in _RULES:
        text = re.sub(pattern, repl, text)
    return text.strip()


def footer(source_number: int, filed: str) -> str:
    """Provenance. Deliberately plain text - a #ref here would autolink."""
    return f"\n\n---\n_Imported from apache/superset issue {source_number}. Originally filed {filed}._"


def assert_clean(text: str, label: str = "") -> None:
    for name, pattern in LEAK_PATTERNS.items():
        found = re.findall(pattern, text)
        if found:
            raise AssertionError(f"{label}: leaked {name}: {found[:5]}")
