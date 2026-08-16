# Automations

Three of these run as native Devin Automations, not through the orchestrator.

**The test is whether there is a branch point.** One event, one session, no
decision in between → Automation. A decision layer — should this get a session
at all, at which tier, against what budget, given what else is in flight → the
orchestrator.

| Automation | Trigger | Branch point? |
|---|---|---|
| `ci-autofix` | a check fails on a `devin/*` branch | No. The failure names the work. |
| `pr-review` | Devin opens a PR | No. Every Devin PR gets reviewed. |
| `dependency-scan` | cron, weekly | No. Scan, file what it finds. |
| *triage* | 20 issues arrive | **Yes** — most of them should not get a session. Orchestrator. |
| *worker dispatch* | issue becomes eligible | **Yes** — tier, ACU ceiling, concurrency cap. Orchestrator. |

The label transitions live **in the Automation prompt**, not in a callback to
this server. That is the point of the thesis: Devin writes to the state machine
directly, as a peer on the bus, rather than reporting to a coordinator that owns
the truth. The orchestrator finds out the same way it finds out about a human's
label change — from GitHub.

The poll loop also adopts sessions these Automations create (`origin:
"automation"`) that it never dispatched, so their ACU burn lands in the same
metrics as everything else. Convergence, not notification.

## Applying

These YAML files are the reviewable definition. Automations are configured in
the Devin UI (Settings → Automations); `make automations` prints each definition
in copy-paste form and checks the trigger/prompt against what is committed here.

`tags` carry the issue/session correlation. Where the trigger payload supports
templating, the issue number comes from the payload; where it does not, it is
recovered from the branch name (`devin/issue-<N>-<slug>`) — which is why that
naming convention is load-bearing and enforced in every worker playbook.
