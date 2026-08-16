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
```

## Verification

Devin writes, CI verifies, Devin fixes what CI catches.

Sessions never set up a development environment, never install dependencies
beyond what the linter for the changed files needs, and never run the test suite.
The repo's own pipeline is the gate — deliberately, because a maintainer trusts
CI, not an agent's self-report. Left alone Devin will `npm ci` unprompted, because
that is what a careful engineer does; the worker playbooks say not to, in those
words.

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
fixtures/             cassettes, the issue snapshots replay runs against, the phase table
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

No `.env`, no GitHub token, no Devin key, no tunnel, no network. The board fills
over about ninety seconds: 22 issues ingested, one scout session triaging the
batch, tiered workers, PRs, red CI, autofix rounds, merges, ACU accumulating,
and the escalations that did not make it.

Everything that distinguishes replay from live lives at the socket, in
`orchestrator/transport.py`. Above that line the clients, the reconciler and the
state machine cannot tell the difference, which is the only thing that makes a
replay worth having as a test.

Under the seam are two things, for two different jobs:

- **A simulated fork and Devin org** (`orchestrator/simulator.py`), seeded from a
  real snapshot of the backlog (`fixtures/source-issues.json`). Reads reflect
  writes, so the reconciler still re-derives the board from "GitHub" the way it
  does live. This is what `make up` runs.
- **Cassettes** (`fixtures/github.jsonl`, `fixtures/devin.jsonl`): JSONL, one
  exchange per line, keyed by method + path + query + body-hash with an
  occurrence counter, so a poll loop that hits one URL five times replays five
  answers in order. `REPLAY_CASSETTE=true` serves from them and nothing else —
  a miss raises rather than improvising.

The cassettes are cut from the simulated fork by `make cassette`, which runs the
scripted timeline in `orchestrator/replay.py`; `tests/test_replay.py` runs that
same script against the cassettes and compares both to the phase table in
`fixtures/timeline.json`. So a state-machine change that reroutes an issue fails
`pytest`, not just the demo. Re-cutting the cassettes is a reviewable diff of
that table.

What replay does **not** cover: real CI (verdicts are assigned by rule, not run),
real review (a simulated reviewer merges green PRs after a beat), Devin's actual
judgement (verdicts come from `seed/issues.yaml`, the human answer key), auth,
rate limits, and the webhook tunnel. Those need the live path below.

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
make simulate    # replay fixtures/webhook-deliveries.json at a running receiver
make cassette    # re-cut fixtures/*.jsonl + timeline.json from the simulated fork
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
