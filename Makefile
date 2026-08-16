.DEFAULT_GOAL := help
COMPOSE ?= docker compose
UV ?= uv

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up: ## Replay the recorded run. No credentials, no ACUs.
	REPLAY=true $(COMPOSE) up --build

.PHONY: live
live: ## Everything live: real fork, real sessions, real spend.
	REPLAY=false $(COMPOSE) --profile live up --build

.PHONY: record
record: ## Live, but wrap outbound HTTP and dump cassettes to fixtures/.
	REPLAY=false RECORD=true $(COMPOSE) up --build

.PHONY: cassette
cassette: ## Re-cut fixtures/*.jsonl from the simulated fork. No credentials, no ACUs.
	$(UV) run python -m orchestrator.replay

.PHONY: down
down: ## Stop everything.
	$(COMPOSE) down -v

.PHONY: seed
seed: ## Labels, then issues, into $$GITHUB_REPO. Idempotent by title.
	$(UV) run python -m orchestrator.seed

.PHONY: github-app
github-app: ## Create the GitHub App (identity + webhook + secret); write it to .env.
	$(UV) run python -m orchestrator.provision

.PHONY: bootstrap
bootstrap: ## Push devin/ playbooks + knowledge notes; write IDs to .env.
	$(UV) run python -m orchestrator.bootstrap

.PHONY: automations
automations: ## Render devin/automations/*.yaml for review before applying.
	$(UV) run python -m orchestrator.automations

.PHONY: simulate
simulate: ## POST recorded webhook payloads (correct HMAC) at the receiver.
	# The sender has to sign with the secret the receiver booted with, and a
	# credential-free receiver booted with the replay one.
	REPLAY=true $(UV) run python -m orchestrator.simulate

.PHONY: dev
dev: ## Backend on :8000 with reload (replay mode).
	REPLAY=true $(UV) run uvicorn orchestrator.main:app --reload --port 8000

.PHONY: web
web: ## Frontend dev server on :5173, proxying /api to :8000.
	cd frontend && npm install && npm run dev

.PHONY: check
check: ## Everything CI runs.
	$(UV) sync --frozen --extra dev
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy orchestrator
	$(UV) run pytest -q
	cd frontend && npm run typecheck && npm run lint
