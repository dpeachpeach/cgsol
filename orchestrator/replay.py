"""One scripted pass through the pipeline, used three ways.

`make cassette` runs it against the simulated fork with `RECORD=true` and writes
`fixtures/github.jsonl` / `fixtures/devin.jsonl`; the test suite runs the same
script against those cassettes with no simulator behind them; and both assert the
same phase table. That is the only arrangement in which a cassette is worth
having: a recording only reproduces a run whose request sequence reproduces, so
the recorder and the replayer have to be one piece of code, not two.

The loops are stepped by hand rather than left to `Poller.start()` for the same
reason. Wall-clock cadences make the request order depend on how busy the machine
is, which a cassette cannot survive.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.config import get_settings
from orchestrator.service import Orchestrator

#: The snapshot the scripted run is seeded from. Six issues, chosen so every
#: branch of the state machine is exercised once and the cassette still fits in
#: a diff: two clean merges, one red-then-fixed, one that stays red until the
#: round cap escalates it, one below the confidence threshold, one declined.
SNAPSHOT = "fixtures/replay-issues.json"


@dataclass(frozen=True)
class Phase:
    """What the board looked like after one step of the script."""

    name: str
    states: dict[int, str] = field(default_factory=dict)
    sessions: int = 0
    acus: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "states": {str(k): v for k, v in self.states.items()},
            "sessions": self.sessions,
            "acus": self.acus,
        }


@asynccontextmanager
async def replay_run(*, cassette: bool = False) -> AsyncIterator[Orchestrator]:
    """An orchestrator wired to the simulated fork (or to the cassettes)."""
    from orchestrator.simulator import reset_world

    previous = {key: os.environ.get(key) for key in _ENV}
    os.environ.update(
        {
            "REPLAY": "true",
            "REPLAY_CASSETTE": "true" if cassette else "false",
            "REPLAY_AUTOSTART": "false",
            "REPLAY_SNAPSHOT": SNAPSHOT,
        }
    )
    get_settings.cache_clear()
    reset_world()
    orchestrator = Orchestrator()
    try:
        yield orchestrator
    finally:
        await orchestrator.github.aclose()
        await orchestrator.devin.aclose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        reset_world()


_ENV = ("REPLAY", "REPLAY_CASSETTE", "REPLAY_AUTOSTART", "REPLAY_SNAPSHOT", "RECORD")


async def run_timeline(orchestrator: Orchestrator) -> list[Phase]:
    """Drive the pipeline from an untouched backlog to a settled board."""
    from orchestrator.simulator import CI_SECONDS, REVIEW_SECONDS, SESSION_SECONDS

    phases: list[Phase] = []
    tick = _ticker(orchestrator)

    def record(name: str) -> None:
        phases.append(_phase(name, orchestrator))

    await orchestrator.poller.reconcile()
    record("ingested")

    await orchestrator.triage_all()
    record("scout-dispatched")

    tick(SESSION_SECONDS + 1)
    await orchestrator.poller.poll_sessions()
    record("triaged")

    await orchestrator.dispatcher.dispatch_ready()
    record("workers-dispatched")

    tick(SESSION_SECONDS + 1)
    await orchestrator.poller.poll_sessions()
    record("prs-opened")

    # One round is a reconcile that reads the checks and dispatches a fixer,
    # then the fixer finishing. The cap plus one so the escalation lands inside
    # the script rather than after it.
    for round_number in range(1, orchestrator.settings.max_ci_rounds + 2):
        await orchestrator.poller.reconcile()
        record(f"ci-verdict-{round_number}")
        tick(CI_SECONDS + 1)
        await orchestrator.poller.poll_sessions()
        record(f"ci-fixed-{round_number}")

    tick(REVIEW_SECONDS + 1)
    await orchestrator.poller.reconcile()
    record("merged")

    await orchestrator.poller.reconcile()
    record("settled")
    return phases


def _ticker(orchestrator: Orchestrator) -> Callable[[float], None]:
    """Step the simulated clock. Against a cassette there is nothing to step —
    the recorded responses already carry the timeline."""
    if orchestrator.settings.replay_cassette:
        return lambda _seconds: None
    from orchestrator.simulator import world

    return world().tick


def _phase(name: str, orchestrator: Orchestrator) -> Phase:
    cards = orchestrator.store.cards()
    return Phase(
        name=name,
        states={card.number: card.state.value if card.state else "-" for card in cards},
        sessions=len(orchestrator.store.sessions()),
        acus=round(sum(card.meta.acus for card in cards), 2),
    )


async def _record() -> int:
    """`make cassette`: cut both cassettes from one scripted run."""
    import json

    os.environ["RECORD"] = "true"
    fixtures = Path(get_settings().fixtures_dir)
    for service in ("github", "devin"):
        (fixtures / f"{service}.jsonl").write_text("", encoding="utf-8")

    async with replay_run() as orchestrator:
        phases = await run_timeline(orchestrator)

    (fixtures / "timeline.json").write_text(
        json.dumps([phase.as_dict() for phase in phases], indent=1) + "\n", encoding="utf-8"
    )
    for phase in phases:
        print(f"{phase.name:<20} {phase.states} sessions={phase.sessions} acu={phase.acus}")
    return 0


def main() -> int:
    import asyncio

    return asyncio.run(_record())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
