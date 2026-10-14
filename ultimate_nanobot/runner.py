"""The runner: the only thing that executes nanobots and the only thing that
decides whether their proposed actions happen.

Order of checks for every run:
  kill switch (file) -> store reachable? (fail-open / fail-closed by avenue)
  -> kill switch (store) -> lifecycle ENABLED? -> work(ctx)
  -> result valid? -> time budget -> produced-not-ran -> action budget
  -> per-action gates (dry-run, approval, dedup, rate limit) -> perform
  -> heartbeat (success only) -> ledger (always).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .core import (
    Action,
    Avenue,
    BudgetExceeded,
    Canary,
    DEFAULT_SINK,
    Lifecycle,
    LLMUnavailable,
    Nanobot,
    NanobotDefinitionError,
    Result,
    validate_name,
)
from .ledger import Ledger
from .sinks import JsonlSink
from .store import SQLiteStore, Store, StoreUnavailable

# run verdicts
OK = "ok"
OK_EMPTY = "ok_empty"
ERROR = "error"
ZERO_OUTPUT = "zero_output"
BAD_RESULT = "bad_result"
BUDGET_EXCEEDED = "budget_exceeded"
OVER_TIME = "over_time"
ACTION_FAILED = "action_failed"
KILLED = "killed"
NOT_ENABLED = "not_enabled"
REFUSED_STORE = "refused_store_unavailable"

SUCCESS_VERDICTS = frozenset({OK, OK_EMPTY})
SKIP_VERDICTS = frozenset({KILLED, NOT_ENABLED})


class _State:
    """ctx.state: per-bot persistent key/value storage backed by the Store."""

    def __init__(self, store: Optional[Store], bot: str, degraded: bool) -> None:
        self._store, self._bot, self._degraded = store, bot, degraded
        self._local: dict = {}

    def get(self, key: str, default: Any = None) -> Any:
        if self._degraded or self._store is None:
            return self._local.get(key, default)
        return self._store.get_state(self._bot, key, default)

    def set(self, key: str, value: Any) -> None:
        if self._degraded or self._store is None:
            self._local[key] = value
            return
        self._store.set_state(self._bot, key, value)


class Context:
    """What a nanobot sees while it works."""

    def __init__(
        self,
        bot: Nanobot,
        *,
        input: dict,
        state: _State,
        home: Path,
        dry_run: bool,
        mode: str,
        llm: Optional[Callable[..., str]],
        run_id: str,
    ) -> None:
        self.bot = bot.name
        self.avenue = bot.avenue_enum
        self.input = dict(input or {})
        self.state = state
        self.home = home
        self.dry_run = dry_run
        self.mode = mode  # live | dry_run | shadow | canary
        self.canary = mode == "canary"
        self.run_id = run_id
        self.budget = bot.effective_budget
        self.llm_calls = 0
        self.logs: list[str] = []
        self._llm = llm
        self._t0 = time.monotonic()

    @property
    def has_llm(self) -> bool:
        return self._llm is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def check_deadline(self) -> None:
        """Call inside long loops: raises BudgetExceeded once max_seconds is spent."""
        if self.elapsed > self.budget.max_seconds:
            raise BudgetExceeded(f"time budget of {self.budget.max_seconds}s spent")

    def llm(self, prompt: str, **kwargs: Any) -> str:
        """Route a prompt to the user-supplied LLM callable, counted against the budget."""
        if self._llm is None:
            raise LLMUnavailable("no LLM configured; nanobots must work without one")
        if self.llm_calls >= self.budget.max_llm_calls:
            raise BudgetExceeded(f"LLM budget of {self.budget.max_llm_calls} calls spent")
        self.check_deadline()
        self.llm_calls += 1
        return self._llm(prompt, **kwargs)

    def log(self, message: str) -> None:
        self.logs.append(str(message))


@dataclass
class RunReport:
    run_id: str
    bot: str
    verdict: str
    mode: str
    produced: int = 0
    artifact: Optional[dict] = None
    actions: list = field(default_factory=list)
    error: Optional[str] = None
    duration_s: float = 0.0
    degraded: bool = False
    heartbeat: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict in SUCCESS_VERDICTS

    @property
    def exit_code(self) -> int:
        if self.ok:
            return 0
        if self.verdict in SKIP_VERDICTS:
            return 3
        return 1


@dataclass
class CanaryReport:
    bot: str
    passed: bool
    detail: str
    report: Optional[RunReport] = None


def _summarise(value: Any, limit: int = 500) -> Any:
    text = json.dumps(value, default=str, sort_keys=True)
    return value if len(text) <= limit else text[:limit] + "...(truncated)"


def _subset_match(expected: Any, actual: Any, path: str = "artifact") -> list[str]:
    """Return mismatches between an expected subset and the actual value."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected a dict, got {type(actual).__name__}"]
        problems = []
        for k, v in expected.items():
            if k not in actual:
                problems.append(f"{path}.{k}: missing")
            else:
                problems.extend(_subset_match(v, actual[k], f"{path}.{k}"))
        return problems
    return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]


class Runner:
    """Runs nanobots against a home directory (store, ledger, failures, flags)."""

    def __init__(
        self,
        home: str | Path = ".nanobot",
        *,
        store: Optional[Store] = None,
        ledger: Optional[Ledger] = None,
        failure_sink: Any = None,
        llm: Optional[Callable[..., str]] = None,
        approver: Optional[Callable[[Action], bool]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.flags_dir = self.home / "flags"
        self.flags_dir.mkdir(exist_ok=True)
        self._store = store
        self._store_error: Optional[str] = None
        self.ledger = ledger or Ledger(self.home / "ledger.jsonl")
        self.failure_sink = failure_sink or JsonlSink(self.home / "failures.jsonl")
        self.fallback_sink = JsonlSink(self.home / "failures.fallback.jsonl")
        self.llm = llm
        self.approver = approver
        self.clock = clock

    # ------------------------------------------------------------------ store
    @property
    def store(self) -> Store:
        """Lazily open the default SQLite store; raises StoreUnavailable if it can't."""
        if self._store is None:
            self._store = SQLiteStore(self.home / "nanobot.db")
        return self._store

    def close(self) -> None:
        """Release the store (needed on Windows before deleting the home dir)."""
        if self._store is not None and hasattr(self._store, "close"):
            self._store.close()

    def __enter__(self) -> "Runner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----------------------------------------------------------- kill switch
    # ------------------------------------------------------ lifecycle cache
    @property
    def cache_path(self) -> Path:
        return self.home / "lifecycle_cache.json"

    def _cache_read(self) -> dict:
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _cache_write(self, name: str, lifecycle: Optional[Lifecycle], avenue: str) -> None:
        """Remember the last lifecycle the store confirmed, so a fail-open bot can
        keep acting through a store outage only if it was ENABLED before it."""
        if lifecycle is None:
            return
        data = self._cache_read()
        entry = {"lifecycle": lifecycle.value, "avenue": avenue}
        if data.get(name) == entry:
            return
        data[name] = entry
        tmp = self.cache_path.with_name(f"{self.cache_path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            tmp.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def kill_file(self, name: Optional[str] = None) -> Path:
        if name is not None:
            validate_name(name)  # a bot name becomes a file name: never a path
        return self.flags_dir / ("KILL_ALL" if name is None else f"KILL_{name}")

    def kill(self, name: Optional[str] = None) -> None:
        self.kill_file(name).write_text(f"killed at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")

    def revive(self, name: Optional[str] = None) -> None:
        path = self.kill_file(name)  # validates the name
        if path.exists():
            path.unlink()  # a switch, not a record: the ledger keeps the history

    def _killed_by_file(self, name: str) -> Optional[str]:
        if self.kill_file().exists():
            return "global kill file"
        if self.kill_file(name).exists():
            return "bot kill file"
        return None

    def _killed_by_store(self, name: str) -> Optional[str]:
        if self.store.get_flag("kill:all"):
            return "global kill flag"
        if self.store.get_flag(f"kill:{name}"):
            return "bot kill flag"
        return None

    # ------------------------------------------------------------ escalation
    def _sink_for(self, bot: Nanobot):
        return self.failure_sink if bot.failure_sink is DEFAULT_SINK else bot.failure_sink

    def escalate(self, bot: Nanobot, kind: str, message: str, run_id: str, **extra: Any) -> None:
        record = {
            "ts": self.clock(),
            "bot": bot.name,
            "avenue": bot.avenue_enum.value,
            "run_id": run_id,
            "kind": kind,
            "message": message,
            **extra,
        }
        try:
            self._sink_for(bot).emit(record)
        except Exception as exc:  # the failure path itself failed: never swallow it
            record["sink_error"] = f"{type(exc).__name__}: {exc}"
            self.fallback_sink.emit(record)
            print(f"[ultimate-nanobot] failure sink error for {bot.name}: {exc}", file=sys.stderr)

    # ------------------------------------------------------------ validation
    def validate(self, bot: Nanobot) -> None:
        if not isinstance(bot, Nanobot):
            raise NanobotDefinitionError(f"{bot!r} is not a Nanobot")
        bot.validate()

    def register(self, bot: Nanobot) -> Lifecycle:
        self.validate(bot)
        return self.store.register(bot.name, bot.avenue_enum.value, bot.cadence)

    # -------------------------------------------------------------- the run
    def run_once(
        self,
        bot: Nanobot,
        *,
        input: Optional[dict] = None,
        live: bool = False,
        shadow: bool = False,
    ) -> RunReport:
        """Run one work unit. `live` overrides a bot's dry-run default.
        `shadow` runs a not-yet-enabled bot without performing any action or
        earning a heartbeat (useful to watch a candidate before promoting it)."""
        self.validate(bot)
        run_id = uuid.uuid4().hex[:12]
        started = self.clock()
        t0 = time.monotonic()
        dry_run = bool(bot.dry_run) and not live
        mode = "shadow" if shadow else ("dry_run" if dry_run else "live")
        report = RunReport(run_id=run_id, bot=bot.name, verdict=ERROR, mode=mode)
        input = dict(input or {})
        use_store = [True]

        def finish(verdict: str, **kw: Any) -> RunReport:
            report.verdict = verdict
            for k, v in kw.items():
                setattr(report, k, v)
            report.duration_s = round(time.monotonic() - t0, 4)
            self._write_ledger(bot, report, input, started, kw.get("llm_calls", 0))
            if use_store[0] and not report.degraded:
                try:
                    self.store.record_run(bot.name, run_id, verdict, report.produced, self.clock())
                except StoreUnavailable:
                    pass  # the ledger already holds the run
            return report

        # 1. kill switch (file) works even with the store down
        reason = self._killed_by_file(bot.name)
        if reason:
            use_store[0] = False  # a file-killed bot never touches the store
            return finish(KILLED, error=reason)

        # 2. store reachable? avenue posture decides
        trusted = True  # may this run perform actions?
        try:
            lifecycle = self.store.register(bot.name, bot.avenue_enum.value, bot.cadence)
            self._cache_write(bot.name, lifecycle, bot.avenue_enum.value)
            reason = self._killed_by_store(bot.name)
        except StoreUnavailable as exc:
            if bot.fails_closed:
                self.escalate(bot, "store_unavailable", f"fail-closed: {exc}", run_id)
                report.degraded = True
                return finish(REFUSED_STORE, error=str(exc))
            report.degraded = True
            reason = None
            cached = self._cache_read().get(bot.name)
            if cached is None:
                # lifecycle unknown: the bot may work, but it may not act
                lifecycle, trusted = None, False
            elif cached.get("avenue") != bot.avenue_enum.value:
                lifecycle = Lifecycle.CANDIDATE
            else:
                lifecycle = Lifecycle(cached.get("lifecycle", Lifecycle.DISCOVERED.value))
        if reason:
            return finish(KILLED, error=reason)

        # 3. lifecycle gate (in degraded mode: the last lifecycle the store confirmed)
        if lifecycle is not None and lifecycle != Lifecycle.ENABLED and not shadow:
            return finish(NOT_ENABLED, error=f"lifecycle is {lifecycle.value}; promote it first")

        # 4. work
        ctx = Context(
            bot,
            input=input,
            state=_State(None if report.degraded else self.store, bot.name, report.degraded),
            home=self.home,
            dry_run=dry_run,
            mode=mode,
            llm=self.llm,
            run_id=run_id,
        )
        try:
            result = bot.work(ctx)
        except BudgetExceeded as exc:
            self.escalate(bot, "budget_exceeded", str(exc), run_id)
            return finish(BUDGET_EXCEEDED, error=str(exc), llm_calls=ctx.llm_calls)
        except BaseException as exc:  # incl. SystemExit: a bot cannot exit the runner silently
            tb = traceback.format_exc()
            self.escalate(bot, "exception", f"{type(exc).__name__}: {exc}", run_id, traceback=tb)
            out = finish(ERROR, error=f"{type(exc).__name__}: {exc}", llm_calls=ctx.llm_calls)
            if isinstance(exc, KeyboardInterrupt):
                raise  # recorded; the operator still gets their Ctrl-C
            return out

        try:
            if not isinstance(result, Result):
                raise TypeError(f"work() must return Result, got {type(result).__name__}")
            result.validate()
        except TypeError as exc:
            self.escalate(bot, "bad_result", str(exc), run_id)
            return finish(BAD_RESULT, error=str(exc), llm_calls=ctx.llm_calls)

        for a in result.actions:
            a.bot = bot.name
        report.produced, report.artifact = result.produced, result.artifact
        report.actions = result.actions
        budget = bot.effective_budget

        # 5. time budget: side effects only happen after work() returns, so an
        #    overrun run never acts.
        if ctx.elapsed > budget.max_seconds:
            self._mark(result.actions, "not_performed", "over time budget")
            msg = f"work took {ctx.elapsed:.2f}s > max_seconds={budget.max_seconds}"
            self.escalate(bot, "over_time", msg, run_id)
            return finish(OVER_TIME, error=msg, llm_calls=ctx.llm_calls)

        # 6. produced, not ran
        verdict = OK
        if result.produced == 0:
            verdict = self._empty_verdict(bot, report.degraded)
            if verdict == ZERO_OUTPUT:
                self._mark(result.actions, "not_performed", "zero-output run")
                msg = "run produced nothing and the bot did not declare empty runs as expected"
                self.escalate(bot, "zero_output", msg, run_id)
                return finish(ZERO_OUTPUT, error=msg, llm_calls=ctx.llm_calls)

        # 7. action budget: a bot that suddenly wants far more than usual is
        #    malfunctioning, so the whole batch is refused.
        if len(result.actions) > budget.max_actions:
            self._mark(result.actions, "not_performed", "over action budget")
            msg = f"{len(result.actions)} actions > max_actions={budget.max_actions}; none performed"
            self.escalate(bot, "budget_exceeded", msg, run_id)
            return finish(BUDGET_EXCEEDED, error=msg, llm_calls=ctx.llm_calls)

        # 8. actions
        if shadow:
            self._mark(result.actions, "not_performed", "shadow run")
        elif not trusted:
            self._mark(result.actions, "not_performed", "store unavailable and lifecycle unknown: not acting")
        else:
            self._process_actions(bot, result.actions, dry_run, run_id)
        failed = [a for a in result.actions if a.status == "failed"]
        if failed:
            msg = f"{len(failed)} action(s) failed: " + "; ".join(a.detail for a in failed)
            self.escalate(bot, "action_failed", msg, run_id)
            return finish(ACTION_FAILED, error=msg, llm_calls=ctx.llm_calls)

        # 9. heartbeat: only now, after a completed work unit
        heartbeat = False
        if not shadow:
            try:
                self.store.heartbeat(bot.name, run_id, self.clock())
                heartbeat = True
            except StoreUnavailable:
                heartbeat = False
        return finish(verdict, heartbeat=heartbeat, llm_calls=ctx.llm_calls)

    def _empty_verdict(self, bot: Nanobot, degraded: bool) -> str:
        """The empty streak is derived from the runner's own runs table, never
        from state the bot can write. Without the store it cannot be verified,
        so an int allowance counts the empty run as a failure."""
        allowed = bot.expected_empty_ok
        if allowed is True:
            return OK_EMPTY
        if allowed is False or allowed == 0 or degraded:
            return ZERO_OUTPUT
        try:
            runs = self.store.recent_runs(bot.name, limit=int(allowed) + 1, verdicts=(OK, OK_EMPTY, ZERO_OUTPUT))
        except StoreUnavailable:
            return ZERO_OUTPUT
        streak = 1  # this run
        for r in runs:
            if r["verdict"] == OK:
                break
            streak += 1
        return OK_EMPTY if streak <= int(allowed) else ZERO_OUTPUT

    @staticmethod
    def _mark(actions: Iterable[Action], status: str, detail: str) -> None:
        for a in actions:
            a.status, a.detail = status, detail

    def _process_actions(self, bot: Nanobot, actions: list, dry_run: bool, run_id: str) -> None:
        now = self.clock()
        for action in actions:
            if dry_run:
                action.status, action.detail = "dry_run", "dry-run: not performed"
                continue
            reservation = None
            try:
                if bot.needs_approval and not self._approved(bot, action, now):
                    continue
                if bot.avenue_enum == Avenue.COMMS:
                    reservation = self._comms_reserve(bot, action, now)
                    if reservation is None:
                        continue
            except StoreUnavailable as exc:  # cannot check the gate -> do not act
                action.status, action.detail = "not_performed", f"store unavailable: {exc}"
                continue
            self._perform(bot, action, now, reservation)

    _APPROVAL_STATUS = {
        "EXECUTED": ("already_executed", "approved action was already performed"),
        "EXECUTING": ("already_executing", "another run holds this approval"),
        "FAILED": ("needs_reapproval", "an earlier attempt failed; approve again to retry"),
        "DENIED": ("denied", "denied by a human"),
    }

    def _approved(self, bot: Nanobot, action: Action, now: float) -> bool:
        """Approval is the Runner's policy, never the bot's. Default: deny until a
        human approves. An APPROVED row is consumed by an atomic claim, so one
        approval performs the action at most once, across processes."""
        status = self.store.request_approval(action.id, bot.name, action.to_dict(), now)
        if status == "PENDING" and self.approver is not None:
            try:
                ok = bool(self.approver(action))
            except Exception as exc:  # an approver error is a denial, never an approval
                ok = False
                action.detail = f"approver raised {type(exc).__name__}: {exc}"
            if not ok:
                action.status = "denied"
                action.detail = action.detail or "approver policy denied"
                return False
            self.store.decide_approval(action.id, "APPROVED", "policy", now)
            status = "APPROVED"
        if status == "APPROVED":
            if self.store.claim_approval(action.id, now):
                return True
            action.status, action.detail = self._APPROVAL_STATUS["EXECUTING"]
            return False
        if status in self._APPROVAL_STATUS:
            action.status, action.detail = self._APPROVAL_STATUS[status]
            return False
        action.status = "pending_approval"
        action.detail = f"awaiting approval: python -m ultimate_nanobot approve {action.id}"
        return False

    def _comms_reserve(self, bot: Nanobot, action: Action, now: float) -> Optional[int]:
        """Dedup + rate limit checked and a slot reserved in one store transaction."""
        verdict, reservation = self.store.reserve_send(
            bot.name, action.dedup_key, now, bot.dedup_window_s, bot.rate_limit_per_hour
        )
        if verdict == "deduped":
            action.status, action.detail = "deduped", "identical message sent within the dedup window"
            return None
        if verdict == "rate_limited":
            action.status, action.detail = "rate_limited", f"over {bot.rate_limit_per_hour} messages/hour"
            return None
        return reservation

    def _perform(self, bot: Nanobot, action: Action, now: float, reservation: Optional[int] = None) -> None:
        interrupted: Optional[BaseException] = None
        if action.perform is None:
            action.status, action.detail = "recorded", "no perform callable; recorded only"
        else:
            try:
                action.perform(action)
                action.status, action.detail = "performed", action.detail or ""
            except BaseException as exc:
                action.status, action.detail = "failed", f"{type(exc).__name__}: {exc}"
                if isinstance(exc, KeyboardInterrupt):
                    interrupted = exc
        ok = action.status != "failed"
        try:
            if bot.needs_approval:
                # FAILED needs a fresh human approval: a payment that may or may
                # not have gone through is never retried automatically.
                self.store.decide_approval(action.id, "EXECUTED" if ok else "FAILED", "runner", now)
            if reservation is not None:
                self.store.finish_send(reservation, "sent" if ok else "failed")
        except StoreUnavailable as exc:
            action.detail = f"{action.detail}; bookkeeping failed: {exc}".lstrip("; ")
        if interrupted is not None:
            raise interrupted

    def _write_ledger(self, bot: Nanobot, report: RunReport, input: dict, started: float, llm_calls: int) -> None:
        self.ledger.append(
            {
                "ts": started,
                "run_id": report.run_id,
                "bot": bot.name,
                "avenue": bot.avenue_enum.value,
                "mode": report.mode,
                "input_summary": _summarise(input),
                "produced": report.produced,
                "artifact": report.artifact,
                "actions": [a.to_dict() for a in report.actions],
                "verdict": report.verdict,
                "error": report.error,
                "duration_s": report.duration_s,
                "llm_calls": llm_calls,
                "heartbeat": report.heartbeat,
                "degraded": report.degraded,
            }
        )

    # --------------------------------------------------- canary + lifecycle
    def run_canary(self, bot: Nanobot, record: bool = True) -> CanaryReport:
        """Run the bot's known-answer probe and grade it HERE, not in the bot.
        A missing, skipped or expectation-free canary is a refusal."""
        self.validate(bot)
        run_id = uuid.uuid4().hex[:12]

        def done(passed: bool, detail: str, rep: Optional[RunReport] = None) -> CanaryReport:
            if record:
                try:
                    self.store.register(bot.name, bot.avenue_enum.value, bot.cadence)
                    self.store.record_canary(bot.name, passed, detail, self.clock())
                except StoreUnavailable:
                    pass
            if not passed:
                self.escalate(bot, "canary_refused", detail, run_id)
            return CanaryReport(bot.name, passed, detail, rep)

        try:
            probe = bot.canary()
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            return done(False, f"refused: canary() raised {type(exc).__name__}: {exc}")
        if probe is None:
            return done(False, "refused: no canary defined")
        if not isinstance(probe, Canary):
            return done(False, "refused: canary() must return a Canary")
        if not probe.has_expectations():
            return done(False, "refused: canary has no expected answer to grade against")

        t0 = time.monotonic()
        ctx = Context(
            bot,
            input=probe.input,
            state=_State(None, bot.name, degraded=True),  # canaries never touch real state
            home=self.home,
            dry_run=True,
            mode="canary",
            llm=self.llm,
            run_id=run_id,
        )
        rep = RunReport(run_id=run_id, bot=bot.name, verdict=ERROR, mode="canary", degraded=True)
        try:
            result = bot.work(ctx)
            if not isinstance(result, Result):
                raise TypeError(f"work() must return Result, got {type(result).__name__}")
            result.validate()
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            rep.error = f"{type(exc).__name__}: {exc}"
            rep.duration_s = round(time.monotonic() - t0, 4)
            self._write_ledger(bot, rep, probe.input, self.clock(), ctx.llm_calls)
            return done(False, f"failed: work raised {rep.error}", rep)

        for a in result.actions:
            a.bot, a.status, a.detail = bot.name, "not_performed", "canary run"
        problems: list[str] = []
        if result.produced == 0:
            problems.append("produced 0 on a known-answer input")
        if probe.expect_produced is not None and result.produced != probe.expect_produced:
            problems.append(f"produced: expected {probe.expect_produced}, got {result.produced}")
        if probe.expect_artifact is not None:
            problems.extend(_subset_match(probe.expect_artifact, result.artifact))
        if probe.expect_action_kinds is not None:
            got = sorted(a.kind for a in result.actions)
            if got != sorted(probe.expect_action_kinds):
                problems.append(f"action kinds: expected {sorted(probe.expect_action_kinds)}, got {got}")
        rep.verdict = OK if not problems else "canary_failed"
        rep.produced, rep.artifact, rep.actions = result.produced, result.artifact, result.actions
        rep.error = "; ".join(problems) or None
        rep.duration_s = round(time.monotonic() - t0, 4)
        self._write_ledger(bot, rep, probe.input, self.clock(), ctx.llm_calls)
        return done(not problems, "passed" if not problems else "failed: " + "; ".join(problems), rep)

    def promote(self, bot: Nanobot) -> CanaryReport:
        """DISCOVERED/CANDIDATE/DISABLED -> CANDIDATE -> (canary passes) -> ENABLED."""
        self.register(bot)
        current = self.store.get_lifecycle(bot.name)
        if current != Lifecycle.CANDIDATE and current != Lifecycle.ENABLED:
            self.store.set_lifecycle(bot.name, Lifecycle.CANDIDATE, "promotion requested")
        rep = self.run_canary(bot)
        if rep.passed:
            if self.store.get_lifecycle(bot.name) != Lifecycle.ENABLED:
                self.store.set_lifecycle(bot.name, Lifecycle.ENABLED, "canary passed")
        elif self.store.get_lifecycle(bot.name) == Lifecycle.ENABLED:
            self.store.set_lifecycle(bot.name, Lifecycle.CANDIDATE, f"canary failed on re-check: {rep.detail}")
        self._cache_write(bot.name, self.store.get_lifecycle(bot.name), bot.avenue_enum.value)
        return rep

    def disable(self, name: str, reason: str = "disabled by operator") -> None:
        validate_name(name)
        self.store.set_lifecycle(name, Lifecycle.DISABLED, reason)
        row = next((b for b in self.store.list_bots() if b["name"] == name), None)
        if row:
            self._cache_write(name, Lifecycle.DISABLED, row["avenue"])

    def approve(self, action_id: str, by: str = "cli") -> dict:
        row = self.store.get_approval(action_id)
        if row is None:
            raise KeyError(f"no pending action {action_id!r}")
        if row["status"] not in ("PENDING", "FAILED"):
            raise ValueError(f"action {action_id} is {row['status']}; only PENDING or FAILED can be approved")
        self.store.decide_approval(action_id, "APPROVED", by, self.clock())
        return self.store.get_approval(action_id)

    def deny(self, action_id: str, by: str = "cli") -> dict:
        row = self.store.get_approval(action_id)
        if row is None:
            raise KeyError(f"no pending action {action_id!r}")
        if row["status"] in ("EXECUTED", "EXECUTING"):
            raise ValueError(f"action {action_id} is {row['status']}; too late to deny")
        self.store.decide_approval(action_id, "DENIED", by, self.clock())
        return self.store.get_approval(action_id)

    # ----------------------------------------------------------------- loop
    def loop(
        self,
        bots: list,
        *,
        max_ticks: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep,
        live: bool = False,
        input: Optional[dict] = None,
        on_report: Optional[Callable[[RunReport], None]] = None,
    ) -> int:
        """Run each bot whenever its cadence comes due. Returns runs performed."""
        for b in bots:
            self.validate(b)
        next_due = {b.name: 0.0 for b in bots}
        ticks = runs = 0
        while max_ticks is None or ticks < max_ticks:
            ticks += 1
            now = self.clock()
            for b in bots:
                if now >= next_due[b.name]:
                    rep = self.run_once(b, live=live, input=input)
                    runs += 1
                    next_due[b.name] = now + float(b.cadence)
                    if on_report:
                        on_report(rep)
            if max_ticks is not None and ticks >= max_ticks:
                break
            wait = max(0.0, min(next_due.values()) - self.clock())
            sleep(min(wait, 60.0))
        return runs


# ---------------------------------------------------------------- loading
def load_bot(spec: str) -> Nanobot:
    """Load 'package.module:attr' or 'path/to/file.py:attr'. A Nanobot subclass
    is instantiated; an instance is returned as-is."""
    if ":" not in spec or spec.endswith(":"):
        raise ValueError(f"bot spec must look like module:attr or file.py:attr, got {spec!r}")
    target, attr = spec.rsplit(":", 1)
    if target.endswith(".py") or "/" in target or "\\" in target:
        path = Path(target).resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        mod_name = f"_nanobot_{path.stem}"
        module = sys.modules.get(mod_name)
        if module is None:
            spec_obj = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec_obj)
            sys.modules[mod_name] = module
            spec_obj.loader.exec_module(module)
    else:
        if "" not in sys.path and str(Path.cwd()) not in sys.path:
            sys.path.insert(0, str(Path.cwd()))
        module = importlib.import_module(target)
    obj = getattr(module, attr)
    if isinstance(obj, type) and issubclass(obj, Nanobot):
        obj = obj()
    if not isinstance(obj, Nanobot):
        raise NanobotDefinitionError(f"{spec} is not a Nanobot")
    return obj
