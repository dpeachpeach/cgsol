# Shared worker contract (inlined into every remediation playbook)

## Verification model — read this before anything else

You write the change. **CI is the verdict.** If CI fails, a follow-up session
fixes what CI reported. That division of labour is deliberate: the maintainer of
this repo trusts their own pipeline, not an agent's self-report. Nothing you run
locally changes whether a PR is accepted — it only changes how likely CI is to
come back red.

So local verification is a cost/benefit call, and the tier makes it for you.
Your tier section below says whether you may run the touched workspace's tests.
Where it says you may not, the rules are absolute:

- **Do not set up the development environment.**
- **Do not install dependencies** beyond what the linter for the changed files
  requires (e.g. `pre-commit`, `ruff`, or `eslint` alone — not the app's full
  dependency tree).
- **Do not run the test suite.** Not a subset of it. Not "just the one file".
- Do not start the app, a database, or a container.

The reasoning, so you can apply it where the tier leaves you discretion: a full
bootstrap costs more than a cheap fix is worth, and CI runs the real thing on
every push anyway. It pays for itself only when a blind fix would plausibly cost
two CI rounds.

**Wall-clock cap: 2 hours for the whole session, whatever you are doing.** If an
install or a test run is still going when you are near it, abandon it, push what
you have, and let CI judge. Never sit watching a dependency install.

## What you do

1. Read enough of the codebase to make a correct, minimal, idiomatic change.
2. Make the change. Match surrounding conventions. Touch only files the issue
   requires.
3. Run lint and type checks **on the changed files only**, plus whatever local
   verification your tier permits.
4. Commit on a branch named `devin/issue-<ISSUE_NUMBER>-<short-slug>` — the
   orchestrator correlates sessions to issues by this convention as well as by
   session tags, so the name is load-bearing.
5. Open a PR against the default branch. The PR body must contain the line
   `Closes #<ISSUE_NUMBER>` and a short explanation of the approach and its
   blast radius.
6. Post one comment on your own PR recording where you landed, in this shape:

   > **Ready to merge** — confidence 0.85. Changed `<files>`; ran `<what you
   > ran>` and it passed. Risk: `<the one thing a reviewer should look at>`.

   Say **Ready to merge** only if you believe the change is complete and
   correct; otherwise say **Needs a human** and why. This is a claim, not a
   verdict — CI still decides, and the orchestrator reads the checks rather
   than this comment. It exists so the reviewer sees your reasoning on the PR
   itself rather than in a session log.
7. Stop. Do not wait for CI, do not poll it, do not fix it. A separate autofix
   session owns that loop.

## When to stop early

If the issue is ambiguous, needs a product decision, or the fix turns out to be
materially larger than its tier suggests, **stop and report** instead of pushing
a speculative change. Say plainly which of these applies:
`ambiguous-requirement`, `missing-context`, `larger-than-tiered`,
`needs-approval`. A clean escalation is cheap; a wrong PR is not.

Your ACU ceiling is a containment mechanism, not a target. If you are close to it
and not close to a PR, that is itself the signal to stop and report. Spending it
on a dependency install rather than on the change is the worst way to hit it.
