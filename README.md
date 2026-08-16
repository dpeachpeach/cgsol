# cgsol — setup

Fork a repo, fill it with a backlog, point cgsol at it, and watch agents file
pull requests against it.

You'll spend about 15 minutes, most of it waiting on a Docker build. Nothing
costs money until the last step, and you have to ask for that twice.

---

## Before you start

- **Docker** + Compose v2, **git**, and [**uv**](https://docs.astral.sh/uv/)
- A **GitHub account** you can log into in a browser
- A **Devin API key** from a service user with the org-level `UseDevinSessions`
  permission, plus its `org-…` id — a personal key works but reports no costs
- Ports `8000`, `5173` and `8765` free

---

## The short version

Four things need a human: the fork, two clicks in a browser, and the last step.
Everything else is a command.

```bash
# 1. Fork github.com/apache/superset to your own account.     ← in the browser

# 2. Clone, and check it runs before any credentials exist.
git clone https://github.com/dpeachpeach/cgsol && cd cgsol
docker compose up -d --build
curl -s localhost:8000/healthz          # "mode":"replay" → open localhost:5173
docker compose down -v

# 3. Fill in .env: your fork, your Devin key.
cp .env.example .env
#   REPLAY=false
#   GITHUB_REPO=<you>/<your-fork>
#   DEVIN_API_KEY=...
#   DEVIN_ORG_ID=org-...
#   TRIAGE_MODE=manual
#   leave MAX_CONCURRENT_WORKERS commented out

# 4. Create the GitHub App. This is also what sets up the webhook:
#    it mints a smee.io channel, points the App's webhook at it, and
#    writes SMEE_URL into .env. Opens a browser.               ← two clicks
make github-app
#   click "Create GitHub App", then "Install" on your fork

# 5. Fill the fork with the backlog. ~22 issues, one at a time.
make seed

# 6. Push the playbooks Devin works from. Free.
make bootstrap

# 7. Start it. `make live` also starts the smee container, which
#    subscribes to your channel and forwards deliveries to the orchestrator.
make live
docker compose logs orchestrator | grep 'starting:'
#   want: replay=False workers=0 triage=TriageMode.MANUAL
docker compose logs smee | tail -2
#   want: Connected https://smee.io/<your-channel>

# 8. Prove the webhook actually works, end to end.
docker compose logs -f orchestrator | grep 'webhook/github'
#    ...then, in the browser: file the held-back issue on your fork and     ← browser
#    label it `needs-triage`. Within a second you should see:
#      POST /webhook/github HTTP/1.1" 200 OK
#    and the card appears on the board without a refresh. Ctrl-C to stop watching.
```

Watch the *orchestrator*, not GitHub's delivery log — smee.io answers 200 whether
or not anything is listening on the other end, so a green delivery in GitHub's UI
proves nothing about the last hop.

No App? A PAT works, with no webhook and a 3-minute lag instead —
see [Do you actually need the GitHub App?](#do-you-actually-need-the-github-app)

The board is now at **localhost:5173**, showing your fork. Nothing has been
dispatched — the cap is 0 and triage is manual, so it will sit there quietly
until you open the two gates below.

## Turn it on

**Triage first** — cheap, one session for the whole batch:

```bash
curl -s -X POST 'localhost:8000/api/triage?estimate=true'   # what will this cost?
curl -s -X POST 'localhost:8000/api/triage'                 # do it
```

Cards move out of the backlog into `devin-eligible`, `devin-declined` or
`can-close-issue`.

**Then workers** — this is the expensive one. Set `MAX_CONCURRENT_WORKERS=1` in
`.env` and `make live` again, or change it in the dashboard's settings dialog.

There is no "go" button. Workers come off a poll loop, so within about a minute
of the cap going above 0, an eligible issue becomes a paid session. Start at 1,
watch one finish, then raise it.

From there it runs on its own: sessions open PRs, CI verdicts move cards to
`human-review`, and a green PR with no escalation gets a `ready-to-merge` label.
Merging is still yours.

## Stopping

```bash
docker compose down -v
```

**This does not stop in-flight Devin sessions** — they keep running and keep
billing. Kill them from the Devin sessions list.

---
---

# Reference

Everything below is context for when something looks wrong, or when you want to
know why a step is shaped the way it is. You don't need it to get running.

## Checking each stage worked

```bash
# after step 4 — the app exists AND is installed on your fork
for k in GITHUB_APP_ID GITHUB_APP_INSTALLATION_ID SMEE_URL; do
  echo -n "$k: "; grep -c "^$k=.\+" .env      # each prints 1
done

# after step 6 — playbook ids were written back
grep -c '^PLAYBOOK_TRIAGE_SCOUT=.\+' .env     # 1

# after step 7 — live, authenticated, and not spending
curl -s localhost:8000/healthz                # "mode":"live"
curl -s localhost:8000/api/config             # max_concurrent_workers: 0
docker compose logs orchestrator | grep -i 'installation token'
docker compose logs orchestrator | grep -c '403'   # 0
docker compose logs smee | tail -2            # Connected https://smee.io/...
```

**If `workers=` is anything but 0 after step 7, stop and find out why.**

The App path has one failure that hides itself: the installation id in `.env` can
point at an installation that no longer exists, and a container that started
*before* the App credentials were written keeps working on the old credential
until you restart. Check it directly:

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

## Do you actually need the GitHub App?

No. The pipeline runs on two timer loops — one polls Devin and dispatches workers
every 60s, the other sweeps GitHub every 180s. Every transition closes on those
sweeps whether or not a webhook ever arrives, and workers are dispatched from the
poll loop, never from an event.

A webhook only buys latency: a check finishing is noticed in about a second
instead of up to three minutes.

| | GitHub App | PAT in `GITHUB_TOKEN` |
| --- | --- | --- |
| board updates | instant | up to 3 min |
| relays payloads via smee.io | yes | no |
| credential | hourly installation token | never-expiring token |
| knows its own writes | yes, as `<slug>[bot]` | no |
| setup | one command, two clicks | paste a token |

On the PAT path, skip step 4 and start with `REPLAY=false docker compose up -d
--build` rather than `make live` — `make live` adds a smee container that needs
an `SMEE_URL` only `make github-app` writes.

A classic PAT needs `repo`. A fine-grained one needs **Issues: read & write**,
**Pull requests: read & write**, **Contents: read**, **Checks: read**,
**Metadata: read**.

That fourth row matters more than it looks: under a PAT the orchestrator's writes
are indistinguishable from yours, so it can't filter out events it caused itself.
Harmless while `TRIAGE_MODE=manual`, because nothing reacts to arriving events —
but load-bearing if you ever switch to `auto`.

### About smee.io

GitHub can't deliver to `localhost`, so `make github-app` mints a channel on the
public smee.io relay and a container subscribes to it. Nothing is exposed
inbound and no ports are forwarded, but your repository's webhook payloads do
pass through a third party.

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
the pipeline live — which is what step 8 uses it for. Filing it is safe: with
`TRIAGE_MODE=manual`, an arriving `needs-triage` issue is recorded and explicitly
*not* scouted, so it costs nothing until you triage.

You can find it in `seed/issues.yaml` as the entry with `hold_back: true`; copy
its title and body onto a new issue.

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
`MAX_CONCURRENT_WORKERS=6 make live` is **silently ignored**. Edit the file.

`.env` holds a private key and an API key. It's gitignored — keep it that way,
and don't paste it into a chat, an issue or a log.

## Gotchas

| symptom | cause |
| --- | --- |
| `The "SMEE_URL" variable is not set` | started the `live` profile without `make github-app`. Expected on the PAT path — start without the profile. |
| `installation token request failed: 404` | stale `GITHUB_APP_INSTALLATION_ID`; reinstalling an app yields a new id. Once an App is configured there is **no fallback** to `GITHUB_TOKEN`, so this is fatal on the next restart. |
| a second app named `<name>-2` | `make github-app` isn't idempotent — it runs the manifest flow every time. To fix an *installation*, install the existing app and re-resolve its id. |
| `make bootstrap --dry-run` acts like a real run | `make` ate the flag. Run `uv run python -m orchestrator.bootstrap --dry-run`. |
| duplicate issues after seeding | idempotency is on **title** only. |
| `403` while seeding | GitHub secondary rate limits. The seeder paces itself; don't remove the delay. |
| board renders but nothing moves | expected before you open the gates: manual triage, cap 0. |
| `403` on every sweep | the GitHub credential is read-only. Needs Issues and Pull requests write. |
| ACU columns empty | personal Devin key, or `DEVIN_ORG_ID` unset → v1 fallback. |
| frontend never starts | it waits on the orchestrator's health check, which waits on the first successful sync. Read the orchestrator logs — it's a credential or repo problem. |
| a setting in `.env` seems ignored | you exported it in the shell, or edited `.env.example`. |
