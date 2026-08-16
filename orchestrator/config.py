"""Configuration. Everything is an environment variable; nothing is read from disk."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- GitHub ---------------------------------------------------------------
    github_repo: str = "dpeachpeach/superset-cg"
    github_token: str = ""
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"

    # Logins whose label writes must never be treated as human intent. Devin
    # writes labels too; without this filter the state machine feeds itself.
    devin_bot_logins: list[str] = Field(default_factory=lambda: ["devin-ai-integration[bot]"])

    # --- Devin ----------------------------------------------------------------
    devin_api_base: str = "https://api.devin.ai"
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_app_base: str = "https://app.devin.ai"

    playbook_triage_scout: str = ""
    playbook_remediate_trivial: str = ""
    playbook_remediate_medium: str = ""
    playbook_remediate_hard: str = ""
    playbook_ci_autofix: str = ""
    knowledge_superset_conventions: str = ""

    # --- Policy ---------------------------------------------------------------
    confidence_threshold: float = 0.6
    max_ci_rounds: int = 3
    max_concurrent_workers: int = 6
    scout_batch_max: int = 25
    acu_ceiling_scout: float = 3
    acu_ceiling_trivial: float = 1.5
    acu_ceiling_medium: float = 3
    acu_ceiling_hard: float = 5
    acu_ceiling_ci_fix: float = 2

    # --- Timing (seconds) -----------------------------------------------------
    batch_window_seconds: float = 60
    poll_active_seconds: float = 25
    poll_waiting_seconds: float = 60
    reconcile_seconds: float = 180
    delivery_ttl_seconds: float = 900

    # --- Modes ----------------------------------------------------------------
    replay: bool = False
    #: Strictly replay recorded cassettes instead of the simulated fork. Only
    #: useful after a `RECORD=true` dress rehearsal; see orchestrator/simulator.py
    #: for why simulation is the default replay.
    replay_cassette: bool = False
    #: In replay there is no webhook to start anything, so triage the backlog
    #: on boot. A dashboard that needs a click before it moves reads as broken.
    replay_autostart: bool = True
    record: bool = False
    fixtures_dir: str = "fixtures"
    tag_namespace: str = "cgsol"
    dry_run: bool = False

    @field_validator("devin_bot_logins", mode="before")
    @classmethod
    def _split_logins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _replay_needs_no_credentials(self) -> Settings:
        """Replay is the mode an evaluator can actually run, so it must not
        require a token to be present, or even plausible."""
        if self.replay:
            self.github_token = self.github_token or "replay"
            self.devin_api_key = self.devin_api_key or "replay"
            self.devin_org_id = self.devin_org_id or "org-replay"
        return self

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/", 1)[1]

    @property
    def live(self) -> bool:
        return not self.replay


@lru_cache
def get_settings() -> Settings:
    return Settings()
