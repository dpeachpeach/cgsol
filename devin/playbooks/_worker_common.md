# Shared worker contract (inlined into every remediation playbook)

## Verification model — read this before anything else

You write the change. **CI verifies it.** If CI fails, a follow-up session fixes
what CI reported. That division of labour is deliberate: the maintainer of this
repo trusts their own pipeline, not an agent's self-report.

Therefore:

- **Do not set up the development environment.**
- **Do not install dependencies** beyond what the linter for the changed files
  requires (e.g. `pre-commit`, `ruff`, or `eslint` alone — not the app's full
  dependency tree).
- **Do not run the test suite.** Not a subset of it. Not "just the one file".
- Do not start the app, a database, or a container.

Left alone you would `npm ci` and run the suite, because that is what a careful
engineer does. Here it is wasted spend: the pipeline runs it for you on every
push, in a correct environment, and its result is the one that counts.

## What you do instead

1. Read enough of the codebase to make a correct, minimal, idiomatic change.
2. Make the change. Match surrounding conventions. Touch only files the issue
   requires.
3. Run lint and type checks **on the changed files only**.
4. Commit on a branch named `devin/issue-<ISSUE_NUMBER>-<short-slug>` — the
   orchestrator correlates sessions to issues by this convention as well as by
   session tags, so the name is load-bearing.
5. Open a PR against the default branch. The PR body must contain the line
   `Closes #<ISSUE_NUMBER>` and a short explanation of the approach and its
   blast radius.
6. Stop. Do not wait for CI, do not poll it, do not fix it. A separate autofix
   session owns that loop.

## When to stop early

If the issue is ambiguous, needs a product decision, or the fix turns out to be
materially larger than its tier suggests, **stop and report** instead of pushing
a speculative change. Say plainly which of these applies:
`ambiguous-requirement`, `missing-context`, `larger-than-tiered`,
`needs-approval`. A clean escalation is cheap; a wrong PR is not.

Your ACU ceiling is a containment mechanism, not a target. If you are close to it
and not close to a PR, that is itself the signal to stop and report.
