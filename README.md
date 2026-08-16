# cgsol

An agent-run pipeline for the backlog nobody gets staffed to: the lint, dependency
and small-fix issues that are individually below the cut line and collectively a
nonzero risk. Issues go in; reviewed pull requests come out, or an explicit
decline with reasoning.

**Just want to see it work?** `make up` — no credentials needed, replays a
recorded run at http://localhost:5173.

---

## The thesis

GitHub is the bus. The orchestrator, Devin and the human are peers on it.

Labels are the state. The metadata blob on the issue is the join. There is no
database, and nothing in this repo is authoritative: kill the orchestrator, start
it again, and it rebuilds its whole world from two API calls in about two seconds.
Three actors write labels concurrently and the state machine is idempotent on the
*current* state rather than on the event that woke it up, which is what makes that
safe.

```text
needs-triage → devin-eligible → devin-working → devin-pr-open → ci-failing ⇄ devin-fixing
                                                             ↘ human-review → done
             ↘ devin-declined                  ↘ devin-blocked
             ↘ can-close-issue
```

`can-close-issue` separates "there is no work here" from "an agent should not do
this work". A stale backlog is full of issues already fixed upstream or
duplicated elsewhere; retiring one costs a few minutes of reading and no code,
which makes it the cheapest thing the pipeline produces. Triage only reaches it
with evidence — the file it read and what that file contains now — and a human
still does the closing.

One label sits outside the state machine: `ready-to-merge`, written on the
**pull request** once its checks are green and the card carries no escalation
other than low confidence. It is not a state — the issue stays `human-review`
until a person merges — and it is derived from the checks the orchestrator read,
not from the worker's sign-off comment, which is a claim rather than a verdict.
It comes off again if CI later goes red.

## Triage cadence

When an untriaged issue becomes a scout session is a spend decision, so it is a
setting rather than a property of the code path:

| mode | behaviour |
| --- | --- |
| `auto` | Each arriving issue is triaged on its webhook, coalesced by the batch window. |
| `chunked` | The untriaged backlog is swept on an interval (`TRIAGE_INTERVAL_SECONDS`, default 30 minutes) — one scout session per sweep instead of one per issue. |
| `manual` | Nothing runs until someone presses *Triage backlog*. The default. |

The sweep re-derives its candidates from GitHub rather than draining an in-memory
queue, so issues that arrive while the orchestrator is down are still in the next
chunk, and switching modes never strands anything.

## Verification

Devin writes, CI verifies, Devin fixes what CI catches.

No session ever stands up Superset itself — no app, no database, no container.
The repo's own pipeline is the gate, deliberately, because a maintainer trusts
CI and not an agent's self-report.

Whether a session may run *tests* is a tier decision, and a cost decision rather
than a correctness one, since CI runs them either way:

| tier | local verification |
| --- | --- |
| `trivial`, CI autofix | lint the changed files, push. A mechanical change that needs a test run was mis-tiered. |
| `medium`, `hard` | may install and run **the touched workspace only** — one frontend package under jest, or `tests/unit_tests/<path>` — when a blind fix would plausibly cost a CI round. |

A local pass buys a lower first-attempt failure rate, nothing more; the PR says
what was run and what it showed. Sessions are capped at two hours of wall clock
on top of their ACU ceiling, so a slow `npm ci` gets abandoned rather than
watched. Whether the trade pays is exactly what the first-attempt failure rate
in the metrics panel is there to answer.

CI autofix is capped at three rounds. A fourth would be an infinite loop with a
budget attached, so round three escalates to `human-review` with
`escalation:ci-unfixable` and the count is kept.

### Workers report in

A worker posts one comment on its issue when it starts implementing:

```text
CGSOL_PROGRESS: drafting-pr Preparing the smallest viable fix.
```

The board shows `drafting PR · 2m ago` from that, and it is the cheapest signal
in the system: Devin's integration writes it on Devin's quota, GitHub delivers it
as a webhook, and the handler projects it straight out of the payload — no issue
read, no PR read, no check read. A progress event that provoked a refetch would
cost more than the polling it replaced, so the tests assert the absence of those
calls rather than trusting the handler to stay honest.

It says what a worker is doing, never what is true: the PR comes from PR
adoption, green comes from checks, and an unrecognised phase is dropped rather
than rendered.

## Cost containment

| session | ceiling |
| --- | --- |
| triage scout (read-only, one per batch) | 3 ACU |
| trivial worker | 1.5 ACU |
| medium worker | 3 ACU |
| hard worker | 5 ACU |
| CI autofix | 2 ACU |

Tight ceilings are failure containment, not thrift: a lint-and-push task burning
3 ACU means something is wrong and you want it stopped rather than finished.

## Layout

```text
devin/playbooks/      agent instructions, versioned — they go through PR review like code
devin/knowledge/      repo conventions pushed to the Devin account by `make bootstrap`
devin/automations/    the three places one event maps to exactly one session
orchestrator/         FastAPI: webhooks, state machine, dispatch, poller, reconciler, metrics
frontend/             Vite + React + Blueprint; reads the orchestrator over SSE, nothing else
seed/                 the corpus and the labels that produced the fork's backlog
fixtures/             recorded traffic + the issue snapshot replay runs against
```

## Orchestration vs. Automations

Automations are right when one event maps to one session. Triage has a decision
layer in between — not every issue should get a session — so it lives in the
orchestrator, along with tier selection, the confidence threshold, concurrency and
budget. Where there is no branch point, there is an Automation:

| Automation | Trigger |
| --- | --- |
| CI autofix | a check fails on `devin/*` |
| PR review | Devin opens a PR |
| Dependency scan | weekly cron, `pip-audit` / `npm audit` → issues |

Their prompts write labels directly. Devin is a first-class writer to the state
machine, not a subordinate reporting through this server; the poller discovers
sessions it never dispatched (`origin: "automation"`) and adopts them.

Three actors write labels, so the receiver has to tell them apart by
`sender.login`: the human, Devin (`devin-ai-integration[bot]`) and the
orchestrator itself. As a PAT the orchestrator wore the human's login and was
indistinguishable from them; as an App it writes as `<app-slug>[bot]`. Both
identities are treated as "not human intent" — they do not count as human turns
on the card — but our own writes still start triage, because
`make seed` files the backlog under exactly that identity.

## Running it

### Replay (default — no credentials)

```bash
make up          # http://localhost:5173
```

Everything that distinguishes replay from live lives at the socket, in
`orchestrator/transport.py`. Above that line the clients, the reconciler and the
state machine cannot tell the difference, which is the only thing that makes a
replay worth having as a test.

### Live

```bash
cp .env.example .env    # DEVIN_API_KEY, DEVIN_ORG_ID
make github-app         # creates the GitHub App: identity, webhook, secret, key
make bootstrap          # push playbooks + knowledge notes, write their IDs back to .env
make live               # includes the smee tunnel, so nothing to install locally
```

#### The GitHub credential

`make github-app` mints a smee channel, serves a page that POSTs a [GitHub App
manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest),
exchanges the code GitHub redirects back with for the app's id, private key and
webhook secret, writes all of it to `.env`, and then waits for the app to be
installed on the fork so it can record the installation id.

The app is the identity *and* the webhook *and* the webhook secret, which is why
this replaces three manual acts with one command: no PAT minted in a settings
page, no webhook created by hand, no secret copied between two browser tabs. At
runtime the orchestrator signs a 10-minute RS256 JWT with the private key and
trades it for an installation token that lives an hour and is refreshed before it
expires — so the standing, never-expiring credential is gone.

**Two clicks remain, and cannot be automated away:**

1. **Create GitHub App** — on the page the manifest is posted to. GitHub requires
   a human account to own an app.
2. **Install** — on the app's install page, granting it the fork. GitHub requires
   the repository owner to consent.

Everything on either side of those two clicks is scripted. If nobody clicks the
second one, the installation poll gives up after ten minutes and tells you the
id to paste in; the app itself is already created and its credentials are already
in `.env`.

`GITHUB_TOKEN` still works and is still the fallback: with no app configured the
client authenticates with the PAT exactly as before, which is what keeps replay
and any half-migrated setup running.

`DEVIN_API_KEY` has to belong to a service user with the org-level
`UseDevinSessions` permission: playbooks, knowledge, dispatch and polling all run
against `/v3/organizations/$DEVIN_ORG_ID/*`, which is where a playbook can carry
its own structured-output schema and where a session reports `acus_consumed` —
without that number there is no cost story. A personal key falls back to the v1
endpoints, and the ACU columns go quiet.

`make seed` files the corpus into a fresh fork. The fork this was demonstrated on
is already seeded; the script exists so the setup is reproducible, and
`seed/issues.yaml` doubles as the answer key replay scores triage against.

### Other targets

```bash
make simulate    # signed webhook deliveries, sent twice each, at a running receiver
make automations # render devin/automations/*.yaml for review before applying
make check       # ruff, mypy, pytest, tsc, eslint
```

## Measuring it

Status is not effectiveness. The board is status; the metrics tab answers "how
would I know this is working":

- **ACU per ready-to-merge PR** — every ACU the pipeline spent, including triage
  and the issues it declined, over the PRs whose CI is green. Cost per outcome
  rather than cost per attempt, which is the number to hold against engineer-hours
- **average age of open issues**, measured from when the bug was first reported
  upstream — the import footer's date, not when the issue was copied onto this
  fork, which would report hours on a backlog that is months old
- **issue → PR** — how long a worker takes, on this fork's clock
- funnel from ingested to merged, and spend-by-tier next to merge-rate-by-tier: if
  hard tier burns 60% of the budget for a 30% merge rate, that is a finding
- escalation taxonomy, which is the input to the next round of knowledge notes

Sessions that built this system are tagged separately from sessions the pipeline
dispatched, so the burn-down is not dominated by "building the thing".
