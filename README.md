# cgsol — setup

Fork a repo, fill it with a backlog, point cgsol at it, and watch agents file
pull requests against it.

About 15 minutes, most of it waiting on a Docker build. Nothing costs money until
the last step, and you have to ask for that twice.

---

## Step-by-step guide

**You need:** Docker with Compose v2, git, [uv](https://docs.astral.sh/uv/), a
browser logged into GitHub, and ports 8000, 5173 and 8765 free.

**Plus a Devin API key** from a service user with the org-level
`UseDevinSessions` permission, and that org's `org-…` id. A personal key works
but reports no costs.

> **Point this at your own repo.** Every command reads `GITHUB_REPO` from `.env`.
> Skip that edit and `docker-compose.yml` falls back to the author's fork, so
> you'll be staring at someone else's backlog wondering why it looks wrong.

### 1. Fork Superset, turn on Actions

Fork `github.com/apache/superset`. Then on the fork's **Actions** tab, click
*"I understand my workflows, go ahead and enable them"* — GitHub disables Actions
on forks, and with no check runs the CI half of the pipeline never fires.

### 2. Clone cgsol, confirm it runs

Before any credentials, so you know the basics work.

```bash
git clone https://github.com/dpeachpeach/cgsol && cd cgsol
docker compose up -d --build
curl -s localhost:8000/healthz     # "mode":"replay"
docker compose down -v
```

A `SMEE_URL is not set` warning is expected — ignore it. The board is at
`localhost:5173`; the frontend lags the orchestrator by ~10s, so retry if the
connection is refused.

### 3. Fill in `.env`

```bash
cp .env.example .env
```

```dotenv
REPLAY=false
GITHUB_REPO=<you>/<your-fork>
DEVIN_API_KEY=...
DEVIN_ORG_ID=org-...
TRIAGE_MODE=manual
```

Leave `MAX_CONCURRENT_WORKERS` commented out. Leave `GITHUB_TOKEN` empty — the
next step replaces it.

### 4. Create the GitHub App

```bash
make github-app
```

Opens a browser. Two clicks are yours: **Create GitHub App**, then **Install**,
granting your fork. Everything else — the webhook, its secret, the private key,
the smee tunnel — is written into `.env` for you.

*(Prefer a token? See [Using a PAT instead](#using-a-pat-instead).)*

### 5. Seed your fork

```bash
make seed
```

Files ~22 issues, one at a time with a pause between. Meant to be slow.

### 6. Push the playbooks

```bash
make bootstrap
```

What Devin works from. Free.

### 7. Start it

```bash
make live
docker compose logs orchestrator | grep 'starting:'
```

Want: `replay=False workers=0 triage=TriageMode.MANUAL`

Board at `localhost:5173`, showing your fork, updating instantly via the webhook.
Nothing is dispatched yet — the cap is 0 and triage is manual.

### 8. Triage (cheap)

One session for the whole batch.

```bash
curl -s -X POST 'localhost:8000/api/triage?estimate=true'   # what will it cost?
curl -s -X POST 'localhost:8000/api/triage'                 # do it
```

Cards move to `devin-eligible`, `devin-declined` or `can-close-issue`.

### 9. Workers (the expensive one)

Set `MAX_CONCURRENT_WORKERS=1` in `.env` and restart, or change it in the
dashboard's settings dialog.

There's no "go" button — workers come off a poll loop, so within a minute of the
cap going above 0 an eligible issue becomes a paid session. Start at 1, watch one
finish, then raise it.

After that it runs itself: sessions open PRs, CI verdicts move cards to
`human-review`, and a green PR with no escalation gets `ready-to-merge`. Merging
is still your call.

### Stopping

```bash
docker compose down -v
```

**In-flight Devin sessions survive this** — they keep running and keep billing.
Kill them from the Devin sessions list.

### Health checks

```bash
grep -c '^PLAYBOOK_TRIAGE_SCOUT=.\+' .env          # after step 6 → 1
curl -s localhost:8000/healthz                     # after step 7 → "mode":"live"
curl -s localhost:8000/api/config                  # max_concurrent_workers: 0
docker compose logs orchestrator | grep -c '403'   # 0
docker compose logs smee | tail -2                 # Connected https://smee.io/...
docker compose logs orchestrator | grep -i 'installation token'
```

**If `workers=` is anything but 0 after step 7, stop and find out why.**

---
---

# *CONTEXT FOR LLMS*

Everything below is for when something looks wrong, or when you want to know why
a step is shaped the way it is. You don't need it to get running.

## The GitHub App

`make github-app` mints a smee.io channel, points the App's webhook at it, and
writes the app id, private key, webhook secret and `SMEE_URL` into `.env`. `make
live` then adds the smee container that subscribes to that channel and forwards
deliveries to the orchestrator — which is why `make live` only works *after*
`make github-app`.

The App is the identity, the webhook and the webhook secret in one object,
replacing a never-expiring token with an installation token that lasts an hour.
It also lets the orchestrator recognise its own writes, because it authors them
as `<slug>[bot]` rather than as you — which matters if you ever switch
`TRIAGE_MODE` to `auto`.

Verify:

```bash
for k in GITHUB_APP_ID GITHUB_APP_INSTALLATION_ID SMEE_URL; do
  echo -n "$k: "; grep -c "^$k=.\+" .env      # each prints 1
done
```

Then prove the webhook end to end: file the held-back issue on your fork, label
it `needs-triage`, and watch.

```bash
docker compose logs -f orchestrator | grep 'webhook/github'
# POST /webhook/github HTTP/1.1" 200 OK
```

The card should appear within a second, without a refresh. Watch the
*orchestrator*, not GitHub's delivery log — smee.io answers 200 whether or not
anything is listening on the other end, so a green delivery in GitHub's UI proves
nothing about the last hop.

### When the App breaks

One failure hides itself: the installation id in `.env` can point at an
installation that no longer exists — reinstalling an app yields a **new** id —
and a container started *before* the App credentials were written keeps working
on the old credential until you restart. Once an App is configured there is **no
fallback** to `GITHUB_TOKEN`, so this is fatal on the next boot.

```bash
uv run python -c "
import asyncio, httpx
from orchestrator.config import Settings
from orchestrator.appauth import build_jwt, build_token_provider
s = Settings()
h = {'Authorization': f'Bearer {build_jwt(s.github_app_id, s.github_app_private_key_pem)}'}
r = httpx.get(f'https://api.github.com/repos/{s.github_repo}/installation', headers=h)
ok = r.status_code == 200
print('repo installation ->', r.status_code, r.json().get('id') if ok else r.text[:80])
if ok:
    asyncio.run(build_token_provider(s).token())
    print('installation token: OK')
"
```

A 404 means the app isn't installed on your fork. Install it at
`https://github.com/apps/<slug>/installations/new` — **don't re-run
`make github-app`**, which creates a second app instead of fixing the first.

### About smee.io

GitHub can't deliver to `localhost`, so the App's webhook points at a channel on
the public smee.io relay and a container subscribes to it. Nothing is exposed
inbound and no ports are forwarded, but your repository's webhook payloads do
pass through a third party.

## Using a PAT instead

The App is optional. The pipeline runs on two timer loops — one polls Devin and
dispatches workers every 60s, the other sweeps GitHub every 180s — and every
transition closes on those sweeps whether or not a webhook arrives. Workers are
dispatched from the poll loop, never from an event. A webhook only buys latency:
a check finishing is noticed in about a second rather than up to three minutes.

To use a token, skip step 4, set `GITHUB_TOKEN` in `.env`, and start with plain
`docker compose up -d --build` rather than `make live` — `make live` starts the
smee container, which needs an `SMEE_URL` only `make github-app` writes.

Mint a fine-grained token at `github.com/settings/personal-access-tokens/new`,
scoped to **only** your fork:

| permission | what needs it |
| --- | --- |
| Issues: read & write | state labels, the metadata comment on each issue |
| Pull requests: read & write | the `ready-to-merge` label |
| Contents: read | `.cgsol/config.yaml` |
| Checks: read | CI verdicts |
| Metadata: read | mandatory on every fine-grained token |

A read-only token populates the board and then logs a `403` on every sweep that
tries to move a card — the board looks fine and nothing ever advances.

Fine-grained tokens are scoped to repositories you pick, so one minted for a
given fork stops working if you later re-point `GITHUB_REPO`, and they **expire**
— a year at most, after which the pipeline 401s. A classic PAT with `repo` also
works.

Note that under a PAT the orchestrator can't distinguish its own writes from
yours. Harmless while `TRIAGE_MODE=manual`, since nothing reacts to arriving
events — load-bearing if you switch to `auto`.

## What seeding does

`make seed` files the label set, then ~22 real issues from `apache/superset`,
sanitized. 15 are expected to be eligible; 6 are decline candidates.

- **Labels before issues**, because the issues reference them.
- **Idempotent on title** — a re-run files nothing twice, so an interrupted seed
  is safe to resume. It will happily duplicate against a fork seeded under
  *different* titles.
- **1–2s between creates.** GitHub answers secondary rate limits with `403`, not
  `429`, so a retry-on-429 client never sees them coming.
- **No tiers, no pipeline labels.** Issues enter at `needs-triage` and triage
  assigns the tier. A seeder that pre-assigns tiers is scoring its own exam.
- **One issue is held back** and deliberately not filed.

Preview it first with `uv run python -m orchestrator.seed --dry-run`.

That held-back issue is there so you can file it by hand later and watch it enter
the pipeline live — it's what the webhook check uses. Filing it is safe: with
`TRIAGE_MODE=manual` an arriving `needs-triage` issue is recorded and explicitly
*not* scouted, so it costs nothing until you triage. Find it in
`seed/issues.yaml` as the entry with `hold_back: true`.

## If you share a Devin org, or re-point at a new repo

Sessions are found by tag: the poller asks Devin for everything tagged `cgsol`
(`poller.py`), and a session is matched to a card by its `issue:N` tag alone
(`state.py`). The repo it belongs to is recorded as a `repo:owner-name` tag but
**is not checked**.

So two deployments sharing a Devin organisation will adopt each other's sessions
wherever issue numbers collide — and since everyone seeds the same corpus into a
fresh repo, those numbers land in near-identical ranges. The symptom is a card
that moves to `devin-pr-open` on its own, with a metadata comment linking a pull
request in someone else's repository.

The same thing happens to one person across time: old sessions stay tagged
forever, so pointing a new fork at the same Devin org adopts the previous run's
history.

Give each deployment its own namespace in `.env`:

```dotenv
TAG_NAMESPACE=cgsol-<your-fork>
```

Set it before the first live boot — changing it later orphans sessions already in
flight, which keep running and keep billing while the board stops tracking them.

## What live mode writes to your repo

There is no read-only live mode. Against `GITHUB_REPO` it writes:

- **state labels** on issues — the label set *is* the state machine
- a **metadata comment** on each issue: tier, attempt, CI rounds, ACU per session
- the **`ready-to-merge` label** on pull requests whose checks are green

Which is why it has to be a fork you own. Never point it at an upstream you
don't control.

## Why nothing spends by accident

Three things must all be true before a Devin session starts, and the defaults
leave all three false:

1. `REPLAY=false` — replay simulates sessions and never reaches the Devin API
2. a Devin key is present
3. `MAX_CONCURRENT_WORKERS` > 0 — **defaults to 0 in live mode**

Triage is the other door, and it's `manual` by default. `auto` scouts on every
arriving webhook and `chunked` on a timer; both spend with nobody watching.

Changing the cap or the cadence from the dashboard applies to the running process
only and reverts to `.env` on restart.

## Config comes from `.env`, not your shell

Compose passes the whole file through `env_file`, but only the few variables
`docker-compose.yml` names can be overridden by an exported shell variable. So
`MAX_CONCURRENT_WORKERS=6 docker compose up` is **silently ignored**. Edit the
file.

`.env` holds an API key and a private key. It's gitignored — keep it that way,
and don't paste it into a chat, an issue or a log.

## Gotchas

| symptom | cause |
| --- | --- |
| board renders but nothing moves | expected before you open the gates: manual triage, cap 0. |
| PRs open but cards never leave `devin-pr-open` | no check runs — Actions are disabled on your fork. Enable them on the Actions tab. Nothing errors; `evaluate_ci` simply has no checks to react to, so `human-review` and `ready-to-merge` are never reached. |
| `installation token request failed: 404` | stale `GITHUB_APP_INSTALLATION_ID`. See [When the App breaks](#when-the-app-breaks). |
| a second app named `<name>-2` | `make github-app` isn't idempotent — it runs the manifest flow every time. To fix an *installation*, install the existing app rather than re-running. |
| `The "SMEE_URL" variable is not set` | **harmless** before step 4. Compose interpolates every service in the file, including the smee one behind the `live` profile, so the warning shows on any compose command until `make github-app` writes it. |
| `403` on every sweep | the credential is read-only. Needs Issues and Pull requests write. |
| `401` on every call, having worked yesterday | a fine-grained token expired, or it isn't scoped to the repo `GITHUB_REPO` now points at. |
| ACU columns empty | personal Devin key, or `DEVIN_ORG_ID` unset → v1 fallback. |
| duplicate issues after seeding | idempotency is on **title** only. |
| `403` while seeding | GitHub secondary rate limits. The seeder paces itself; don't remove the delay. |
| frontend never starts | it waits on the orchestrator's health check, which waits on the first successful sync. Read the orchestrator logs — it's a credential or repo problem. |
| a setting in `.env` seems ignored | you exported it in the shell, or edited `.env.example`. |
| a card moves on its own, linking a PR in a **different** repo | another deployment's session was adopted — sessions match on issue number, not repo. Set a unique `TAG_NAMESPACE`. |
| a second clone takes over the first one's containers | the Compose project name is hardcoded (`name: cgsol`) and ports 8000/5173 are fixed, so **only one instance can run at a time**. `docker compose down` the first before starting the second. |
| `make bootstrap --dry-run` acts like a real run | `make` ate the flag. Run `uv run python -m orchestrator.bootstrap --dry-run`. |

