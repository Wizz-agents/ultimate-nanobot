"""Cadence-relative liveness verdicts computed from heartbeat history."""

from __future__ import annotations

import time
from typing import Optional

OK = "OK"
LATE = "LATE"
DARK = "DARK"
NEVER = "NEVER"

LATE_FACTOR = 1.5
DARK_FACTOR = 3.0


def verdict(
    last_heartbeat: Optional[float],
    cadence: float,
    now: Optional[float] = None,
    late_factor: float = LATE_FACTOR,
    dark_factor: float = DARK_FACTOR,
) -> str:
    """OK if the last earned heartbeat is within 1.5x cadence, LATE up to 3x,
    DARK beyond, NEVER if the bot has never completed a work unit.

    A 24 h job that beat 2 h ago is fine; a 60 s job that beat 2 h ago is dead.
    That is why the thresholds scale with cadence instead of being absolute."""
    if last_heartbeat is None:
        return NEVER
    now = time.time() if now is None else now
    age = max(0.0, now - last_heartbeat)
    if age <= cadence * late_factor:
        return OK
    if age <= cadence * dark_factor:
        return LATE
    return DARK


def fleet_status(store, now: Optional[float] = None) -> list[dict]:
    """One row per registered bot: lifecycle, last heartbeat age, liveness verdict, last run verdict."""
    now = time.time() if now is None else now
    rows = []
    for bot in store.list_bots():
        beats = store.heartbeats(bot["name"], limit=1)
        last = beats[0] if beats else None
        run = store.last_run(bot["name"])
        rows.append(
            {
                "name": bot["name"],
                "avenue": bot["avenue"],
                "cadence": bot["cadence"],
                "lifecycle": bot["lifecycle"],
                "last_heartbeat_age_s": None if last is None else round(now - last, 1),
                "liveness": verdict(last, bot["cadence"], now),
                "last_verdict": run["verdict"] if run else None,
                "killed": bool(store.get_flag(f"kill:{bot['name']}") or store.get_flag("kill:all")),
            }
        )
    return rows
