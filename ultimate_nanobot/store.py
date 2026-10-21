"""Storage. A small Store interface with an SQLite default (stdlib sqlite3).

Plug in another backend (Postgres, Redis, an HTTP API) by subclassing Store.
Every method must raise StoreUnavailable when the backend cannot be reached;
the runner turns that into fail-open or fail-closed behaviour per avenue.
"""

from __future__ import annotations

import abc
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .core import Lifecycle


class StoreUnavailable(Exception):
    """The store cannot be reached. Avenue posture decides what happens next."""


class Store(abc.ABC):
    """The persistence contract every backend implements."""

    # --- fleet registry / lifecycle ---
    @abc.abstractmethod
    def register(self, name: str, avenue: str, cadence: float) -> Lifecycle:
        """Upsert a bot. New bots start DISCOVERED. If the avenue changed since the
        bot was promoted, the bot drops back to CANDIDATE (it must re-earn ENABLED
        under its new posture). Returns the current lifecycle."""

    @abc.abstractmethod
    def get_lifecycle(self, name: str) -> Optional[Lifecycle]: ...

    @abc.abstractmethod
    def set_lifecycle(self, name: str, state: Lifecycle, reason: str) -> None:
        """Change state and append a row to the (append-only) transition log."""

    @abc.abstractmethod
    def transitions(self, name: str) -> list[dict]: ...

    @abc.abstractmethod
    def list_bots(self) -> list[dict]: ...

    # --- heartbeats + run outcomes ---
    @abc.abstractmethod
    def heartbeat(self, name: str, run_id: str, ts: float) -> None: ...

    @abc.abstractmethod
    def heartbeats(self, name: str, limit: int = 50) -> list[float]:
        """Most recent first."""

    @abc.abstractmethod
    def record_run(self, name: str, run_id: str, verdict: str, produced: int, ts: float) -> None: ...

    @abc.abstractmethod
    def last_run(self, name: str) -> Optional[dict]: ...

    @abc.abstractmethod
    def recent_runs(self, name: str, limit: int = 50, verdicts: Optional[tuple] = None) -> list[dict]:
        """Most recent first (optionally only these verdicts): dicts with at least verdict and produced."""

    # --- per-bot key/value state ---
    @abc.abstractmethod
    def get_state(self, name: str, key: str, default: Any = None) -> Any: ...

    @abc.abstractmethod
    def set_state(self, name: str, key: str, value: Any) -> None: ...

    # --- flags (kill switch etc.) ---
    @abc.abstractmethod
    def get_flag(self, key: str) -> Optional[str]: ...

    @abc.abstractmethod
    def set_flag(self, key: str, value: Optional[str]) -> None: ...

    # --- approvals ---
    @abc.abstractmethod
    def request_approval(self, action_id: str, bot: str, action: dict, ts: float) -> str:
        """Create a PENDING approval if none exists. Returns the current status."""

    @abc.abstractmethod
    def get_approval(self, action_id: str) -> Optional[dict]: ...

    @abc.abstractmethod
    def decide_approval(self, action_id: str, status: str, by: str, ts: float) -> None: ...

    @abc.abstractmethod
    def claim_approval(self, action_id: str, ts: float) -> bool:
        """Atomically move APPROVED -> EXECUTING. True only for the single caller
        that won the claim; that caller (and only it) may perform the action."""

    @abc.abstractmethod
    def pending_approvals(self) -> list[dict]: ...

    @abc.abstractmethod
    def stuck_approvals(self, older_than_s: float, now: float) -> list[dict]:
        """Approvals EXECUTING for longer than ``older_than_s``: a runner claimed them and never reported a result."""

    @abc.abstractmethod
    def release_approval(self, action_id: str, by: str, ts: float) -> bool:
        """Atomically move EXECUTING -> FAILED (a fresh human approval is then needed). True only if it was EXECUTING."""

    # --- comms dedup / rate limit ---
    @abc.abstractmethod
    def reserve_send(
        self, bot: str, key: str, ts: float, dedup_window_s: float, per_hour: int
    ) -> tuple[str, Optional[int]]:
        """Atomically check dedup + rate limit and, if allowed, reserve a slot.
        Returns ("ok", reservation_id) | ("deduped", None) | ("rate_limited", None)."""

    @abc.abstractmethod
    def finish_send(self, reservation_id: int, status: str) -> None:
        """Mark a reservation 'sent' or 'failed' (failed slots stop counting)."""

    @abc.abstractmethod
    def record_sent(self, bot: str, key: str, ts: float) -> None: ...

    @abc.abstractmethod
    def last_sent(self, bot: str, key: str) -> Optional[float]: ...

    @abc.abstractmethod
    def count_sent_since(self, bot: str, since: float) -> int: ...

    # --- canary ---
    @abc.abstractmethod
    def record_canary(self, name: str, passed: bool, detail: str, ts: float) -> None: ...

    def ping(self) -> None:
        """Raise StoreUnavailable if the backend is down."""
        self.get_flag("__ping__")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    name TEXT PRIMARY KEY, avenue TEXT NOT NULL, cadence REAL NOT NULL,
    lifecycle TEXT NOT NULL, registered_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, from_state TEXT,
    to_state TEXT NOT NULL, reason TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, run_id TEXT, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS heartbeats_name_ts ON heartbeats(name, ts);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, run_id TEXT NOT NULL,
    verdict TEXT NOT NULL, produced INTEGER NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS runs_name_ts ON runs(name, ts);
CREATE TABLE IF NOT EXISTS state (
    name TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (name, key));
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    action_id TEXT PRIMARY KEY, bot TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
    requested_at REAL NOT NULL, decided_at REAL, decided_by TEXT);
CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent');
CREATE INDEX IF NOT EXISTS sent_log_bot_ts ON sent_log(bot, ts);
CREATE TABLE IF NOT EXISTS canary_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, passed INTEGER NOT NULL,
    detail TEXT, ts REAL NOT NULL);
"""


class SQLiteStore(Store):
    """Default store: one SQLite file, safe to share across threads in-process."""

    _lock = threading.RLock()  # per-instance lock set in __init__; class default for subclasses

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            # autocommit mode: single statements commit at once; multi-statement
            # critical sections use explicit BEGIN IMMEDIATE (see _tx).
            self._conn = sqlite3.connect(
                self.path, check_same_thread=False, timeout=30, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise StoreUnavailable(f"cannot open store {self.path}: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = (), fetch: str = "none"):
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                if fetch == "one":
                    row = cur.fetchone()
                    out = dict(row) if row else None
                elif fetch == "all":
                    out = [dict(r) for r in cur.fetchall()]
                elif fetch == "rowcount":
                    out = cur.rowcount
                else:
                    out = None
                return out  # autocommit mode: nothing to commit here
            except sqlite3.Error as exc:
                raise StoreUnavailable(str(exc)) from exc

    def _tx(self, fn):
        """Run fn() inside BEGIN IMMEDIATE ... COMMIT (a cross-process write lock)."""
        with self._lock:
            self._exec("BEGIN IMMEDIATE")
            try:
                out = fn()
            except BaseException:
                try:
                    self._exec("ROLLBACK")
                except StoreUnavailable:
                    pass
                raise
            self._exec("COMMIT")
            return out

    # registry / lifecycle
    def register(self, name: str, avenue: str, cadence: float) -> Lifecycle:
        return self._tx(lambda: self._register(name, avenue, cadence))

    def _register(self, name: str, avenue: str, cadence: float) -> Lifecycle:
        now = time.time()
        row = self._exec("SELECT lifecycle, avenue FROM bots WHERE name=?", (name,), "one")
        if row is None:
            self._exec(
                "INSERT INTO bots(name, avenue, cadence, lifecycle, registered_at, updated_at) VALUES (?,?,?,?,?,?)",
                (name, avenue, float(cadence), Lifecycle.DISCOVERED.value, now, now),
            )
            self._exec(
                "INSERT INTO transitions(name, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)",
                (name, None, Lifecycle.DISCOVERED.value, "registered", now),
            )
            return Lifecycle.DISCOVERED
        state = Lifecycle(row["lifecycle"])
        if row["avenue"] != avenue:
            new_state = Lifecycle.CANDIDATE if state is Lifecycle.ENABLED else state
            self._exec(
                "UPDATE bots SET avenue=?, cadence=?, lifecycle=?, updated_at=? WHERE name=?",
                (avenue, float(cadence), new_state.value, now, name),
            )
            self._exec(
                "INSERT INTO transitions(name, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)",
                (name, state.value, new_state.value, f"avenue changed {row['avenue']} -> {avenue}; re-promote", now),
            )
            return new_state
        self._exec("UPDATE bots SET cadence=? WHERE name=?", (float(cadence), name))
        return state

    def get_lifecycle(self, name: str) -> Optional[Lifecycle]:
        row = self._exec("SELECT lifecycle FROM bots WHERE name=?", (name,), "one")
        return Lifecycle(row["lifecycle"]) if row else None

    def set_lifecycle(self, name: str, state: Lifecycle, reason: str) -> None:
        now = time.time()
        prev = self.get_lifecycle(name)
        if prev is None:
            raise KeyError(f"unknown bot {name!r}")
        self._exec("UPDATE bots SET lifecycle=?, updated_at=? WHERE name=?", (state.value, now, name))
        self._exec(
            "INSERT INTO transitions(name, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)",
            (name, prev.value, state.value, reason, now),
        )

    def transitions(self, name: str) -> list[dict]:
        return self._exec("SELECT * FROM transitions WHERE name=? ORDER BY id", (name,), "all")

    def list_bots(self) -> list[dict]:
        return self._exec("SELECT * FROM bots ORDER BY name", (), "all")

    # heartbeats / runs
    def heartbeat(self, name: str, run_id: str, ts: float) -> None:
        self._exec("INSERT INTO heartbeats(name, run_id, ts) VALUES (?,?,?)", (name, run_id, ts))

    def heartbeats(self, name: str, limit: int = 50) -> list[float]:
        rows = self._exec(
            "SELECT ts FROM heartbeats WHERE name=? ORDER BY ts DESC LIMIT ?", (name, limit), "all"
        )
        return [r["ts"] for r in rows]

    def record_run(self, name: str, run_id: str, verdict: str, produced: int, ts: float) -> None:
        self._exec(
            "INSERT INTO runs(name, run_id, verdict, produced, ts) VALUES (?,?,?,?,?)",
            (name, run_id, verdict, int(produced), ts),
        )

    def last_run(self, name: str) -> Optional[dict]:
        return self._exec("SELECT * FROM runs WHERE name=? ORDER BY id DESC LIMIT 1", (name,), "one")

    def recent_runs(self, name: str, limit: int = 50, verdicts: Optional[tuple] = None) -> list[dict]:
        if not verdicts:
            return self._exec("SELECT * FROM runs WHERE name=? ORDER BY id DESC LIMIT ?", (name, limit), "all")
        marks = ",".join("?" for _ in verdicts)
        return self._exec(
            f"SELECT * FROM runs WHERE name=? AND verdict IN ({marks}) ORDER BY id DESC LIMIT ?",
            (name, *verdicts, limit),
            "all",
        )

    # state
    def get_state(self, name: str, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM state WHERE name=? AND key=?", (name, key), "one")
        return json.loads(row["value"]) if row else default

    def set_state(self, name: str, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO state(name, key, value) VALUES (?,?,?) "
            "ON CONFLICT(name, key) DO UPDATE SET value=excluded.value",
            (name, key, json.dumps(value, default=str)),
        )

    # flags
    def get_flag(self, key: str) -> Optional[str]:
        row = self._exec("SELECT value FROM flags WHERE key=?", (key,), "one")
        return row["value"] if row else None

    def set_flag(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self._exec("DELETE FROM flags WHERE key=?", (key,))  # a flag, not a record
        else:
            self._exec(
                "INSERT INTO flags(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # approvals
    def request_approval(self, action_id: str, bot: str, action: dict, ts: float) -> str:
        self._exec(
            "INSERT OR IGNORE INTO approvals(action_id, bot, action, status, requested_at) VALUES (?,?,?,?,?)",
            (action_id, bot, json.dumps(action, default=str), "PENDING", ts),
        )
        return self.get_approval(action_id)["status"]

    def claim_approval(self, action_id: str, ts: float) -> bool:
        n = self._exec(
            "UPDATE approvals SET status='EXECUTING', decided_at=?, decided_by='runner' "
            "WHERE action_id=? AND status='APPROVED'",
            (ts, action_id),
            "rowcount",
        )
        return n == 1

    def get_approval(self, action_id: str) -> Optional[dict]:
        row = self._exec("SELECT * FROM approvals WHERE action_id=?", (action_id,), "one")
        if row:
            row["action"] = json.loads(row["action"])
        return row

    def decide_approval(self, action_id: str, status: str, by: str, ts: float) -> None:
        if self.get_approval(action_id) is None:
            raise KeyError(f"no approval request with id {action_id!r}")
        self._exec(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE action_id=?",
            (status, ts, by, action_id),
        )

    def pending_approvals(self) -> list[dict]:
        rows = self._exec("SELECT * FROM approvals WHERE status='PENDING' ORDER BY requested_at", (), "all")
        for r in rows:
            r["action"] = json.loads(r["action"])
        return rows

    def stuck_approvals(self, older_than_s: float, now: float) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM approvals WHERE status='EXECUTING' AND decided_at <= ? ORDER BY decided_at",
            (now - older_than_s,),
            "all",
        )
        for r in rows:
            r["action"] = json.loads(r["action"])
        return rows

    def release_approval(self, action_id: str, by: str, ts: float) -> bool:
        n = self._exec(
            "UPDATE approvals SET status='FAILED', decided_at=?, decided_by=? WHERE action_id=? AND status='EXECUTING'",
            (ts, by, action_id),
            "rowcount",
        )
        return n == 1

    # comms
    def reserve_send(self, bot: str, key: str, ts: float, dedup_window_s: float, per_hour: int):
        def body():
            row = self._exec(
                "SELECT MAX(ts) AS ts FROM sent_log WHERE bot=? AND key=? AND status!='failed'", (bot, key), "one"
            )
            if row and row["ts"] is not None and ts - row["ts"] < dedup_window_s:
                return "deduped", None
            row = self._exec(
                "SELECT COUNT(*) AS n FROM sent_log WHERE bot=? AND ts>=? AND status!='failed'",
                (bot, ts - 3600),
                "one",
            )
            if int(row["n"]) >= per_hour:
                return "rate_limited", None
            self._exec("INSERT INTO sent_log(bot, key, ts, status) VALUES (?,?,?,'reserved')", (bot, key, ts))
            return "ok", self._exec("SELECT last_insert_rowid() AS id", (), "one")["id"]

        return self._tx(body)

    def finish_send(self, reservation_id: int, status: str) -> None:
        self._exec("UPDATE sent_log SET status=? WHERE id=?", (status, reservation_id))

    def record_sent(self, bot: str, key: str, ts: float) -> None:
        self._exec("INSERT INTO sent_log(bot, key, ts) VALUES (?,?,?)", (bot, key, ts))

    def last_sent(self, bot: str, key: str) -> Optional[float]:
        row = self._exec(
            "SELECT MAX(ts) AS ts FROM sent_log WHERE bot=? AND key=? AND status!='failed'", (bot, key), "one"
        )
        return row["ts"] if row and row["ts"] is not None else None

    def count_sent_since(self, bot: str, since: float) -> int:
        row = self._exec(
            "SELECT COUNT(*) AS n FROM sent_log WHERE bot=? AND ts>=? AND status!='failed'", (bot, since), "one"
        )
        return int(row["n"])

    # canary
    def record_canary(self, name: str, passed: bool, detail: str, ts: float) -> None:
        self._exec(
            "INSERT INTO canary_results(name, passed, detail, ts) VALUES (?,?,?,?)",
            (name, 1 if passed else 0, detail, ts),
        )
