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
cp .env.example .env    # GITHUB_TOKEN, DEVIN_API_KEY, DEVIN_ORG_ID, SMEE_URL
make bootstrap          # push playbooks + knowledge notes, write their IDs back to .env
make live               # includes the smee tunnel, so nothing to install locally
```

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

- **autonomy rate** — merged PRs that needed zero human turns
- **ACU per merged PR**, trended
- **CI rounds to green** — first-pass quality, and the number that should fall as
  the playbooks get tuned
- funnel from ingested to merged, and spend-by-tier next to merge-rate-by-tier: if
  hard tier burns 60% of the budget for a 30% merge rate, that is a finding
- escalation taxonomy, which is the input to the next round of knowledge notes

Sessions that built this system are tagged separately from sessions the pipeline
dispatched, so the burn-down is not dominated by "building the thing".
