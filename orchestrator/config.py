"""Configuration. Everything is an environment variable; nothing is read from disk."""

from __future__ import annotations

import base64
import binascii
from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TriageMode(str, Enum):
    """When an untriaged issue turns into a scout session.

    ``AUTO`` reacts to the webhook, ``CHUNKED`` lets issues pile up and spends
    once per interval, ``MANUAL`` only ever triages when a human asks. The
    difference is entirely about who decides when money is spent.
    """

    AUTO = "auto"
    CHUNKED = "chunked"
    MANUAL = "manual"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- GitHub ---------------------------------------------------------------
    github_repo: str = "dpeachpeach/superset-cg"
    github_token: str = ""
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"
    #: The tunnel GitHub delivers to. `make github-app` mints one when unset.
    smee_url: str = ""

    # --- GitHub App -----------------------------------------------------------
    # Written by `make github-app`. When these are set the orchestrator mints
    # short-lived installation tokens for itself and `github_token` is unused;
    # when they are not, the PAT is still the credential.
    github_app_id: str = ""
    github_app_slug: str = ""
    #: The PEM, base64-encoded so it survives a single .env line. A literal or
    #: \n-escaped PEM is accepted too, for a key pasted in by hand.
    github_app_private_key: str = ""
    github_app_installation_id: str = ""
    #: Refresh this many seconds before the installation token actually expires;
    #: a token that dies mid-request costs a retry and a confusing 401 in a log.
    github_app_token_skew_seconds: float = 300

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

    # --- Triage cadence -------------------------------------------------------
    #: Manual by default: an orchestrator that starts spending ACUs the moment
    #: it can see a backlog is not a demo anyone wants to run twice.
    triage_mode: TriageMode = TriageMode.MANUAL
    triage_interval_seconds: float = 1800

    # --- Policy ---------------------------------------------------------------
    #: A verdict below this is routed to a human instead of a worker. Off by
    #: default: the analyst's own eligibility call is the gate, and a confident
    #: wrong answer is not less likely than a hesitant right one. Raise it to
    #: buy review at the cost of throughput.
    confidence_threshold: float = 0.0
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
    def github_app_configured(self) -> bool:
        return bool(self.github_app_id and self.github_app_private_key)

    @property
    def github_app_login(self) -> str:
        """How the App authors events. An App's writes arrive as `<slug>[bot]`,
        which is a different sender from the PAT's human login."""
        return f"{self.github_app_slug}[bot]" if self.github_app_slug else ""

    @property
    def github_app_private_key_pem(self) -> str:
        """The PEM, however it was stored. Never log the result."""
        raw = self.github_app_private_key.strip()
        if not raw:
            return ""
        if "-----BEGIN" in raw:
            decoded = raw.replace("\\n", "\n")
        else:
            try:
                decoded = base64.b64decode(raw, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ValueError("GITHUB_APP_PRIVATE_KEY is neither a PEM nor base64") from exc
        return decoded if decoded.endswith("\n") else decoded + "\n"

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
