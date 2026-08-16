"""Settings apply to the running process without touching GitHub.

They used to be committed to the fork, on the theory that every change should
be reviewable. In practice the control that matters is the spend cap, and
routing "stop spending" through a commit made it fail exactly when GitHub was
unhappy — which is when the orchestrator is most likely to be misbehaving.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings, TriageMode
from orchestrator.service import Orchestrator
from orchestrator.state import Store


class ForbiddenGitHub:
    """Any call here is the bug this test exists to catch."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"applying settings must not call GitHub ({name})")


def service_for() -> Orchestrator:
    service = Orchestrator.__new__(Orchestrator)
    service.settings = Settings(replay=True, max_concurrent_workers=4)
    service.store = Store()
    service.github = ForbiddenGitHub()  # type: ignore[assignment]
    return service


async def test_the_cap_takes_effect_without_a_commit() -> None:
    service = service_for()
    applied = await service.apply_config({"max_concurrent_workers": 0})
    assert applied == {"max_concurrent_workers": 0}
    assert service.settings.max_concurrent_workers == 0


async def test_a_cadence_change_still_reaches_the_frontend() -> None:
    service = service_for()
    seen: list[tuple[str, Any]] = []

    async def publish(kind: str, body: Any) -> None:
        seen.append((kind, body))

    service.store.publish = publish  # type: ignore[method-assign]
    service._restart_chunk_loop = lambda: None  # type: ignore[method-assign]

    await service.apply_config({"triage_mode": "manual"})
    assert service.settings.triage_mode is TriageMode.MANUAL
    assert seen[0][0] == "config.reloaded"


def test_live_does_not_assume_permission_to_spend() -> None:
    """A fresh clone pointed at a real fork dispatches nothing on its own: the
    poll loop calls `dispatch_ready`, so any other default spends on boot."""
    assert Settings(replay=False, _env_file=None).max_concurrent_workers == 0
    explicit = Settings(replay=False, max_concurrent_workers=3, _env_file=None)
    assert explicit.max_concurrent_workers == 3
    # Replay sessions are simulated, so the demo still moves.
    assert Settings(replay=True, _env_file=None).max_concurrent_workers > 0


async def test_unknown_keys_cannot_reach_credentials_or_endpoints() -> None:
    service = service_for()
    base = service.settings.devin_api_base
    await service.apply_config({"devin_api_base": "https://exfil.example", "dry_run": True})
    assert service.settings.devin_api_base == base
    assert service.settings.dry_run is True
