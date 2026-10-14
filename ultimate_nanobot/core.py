"""Core types: avenues, lifecycle states, results, actions, budgets and the Nanobot itself."""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union


class Avenue(str, enum.Enum):
    """What a nanobot serves. The avenue decides its safety posture."""

    MONEY = "MONEY"
    SYSTEM = "SYSTEM"
    KNOWLEDGE = "KNOWLEDGE"
    SECURITY = "SECURITY"
    COMMS = "COMMS"
    GENERAL = "GENERAL"

    @classmethod
    def parse(cls, value: Union[str, "Avenue"]) -> "Avenue":
        if isinstance(value, Avenue):
            return value
        try:
            return cls(str(value).upper())
        except ValueError:
            valid = ", ".join(a.value for a in cls)
            raise NanobotDefinitionError(f"unknown avenue {value!r}; expected one of: {valid}") from None


class Lifecycle(str, enum.Enum):
    """A nanobot's right to run. Only ENABLED bots act for real."""

    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


# Avenues that refuse to run when the store cannot be reached. COMMS is here
# because its dedup/rate-limit gates live in the store; it may opt into
# fail-open (and then sends nothing while the store is down).
FAIL_CLOSED_AVENUES = frozenset({Avenue.MONEY, Avenue.SECURITY, Avenue.COMMS})
# Avenues whose every action needs an approval (default: deny until a human approves).
APPROVAL_AVENUES = frozenset({Avenue.MONEY, Avenue.SECURITY})
# Fail-closed avenues that cannot be overridden to fail-open.
LOCKED_CLOSED_AVENUES = frozenset({Avenue.MONEY, Avenue.SECURITY})


class NanobotError(Exception):
    """Base class for framework errors."""


class NanobotDefinitionError(NanobotError):
    """The nanobot definition is invalid and cannot be registered."""


class BudgetExceeded(NanobotError):
    """A per-run budget cap (actions, LLM calls, seconds) was hit."""


class LLMUnavailable(NanobotError):
    """ctx.llm() was called but no LLM callable is configured."""


class SkipCanary(NanobotError):
    """Raised by a canary() that cannot run. The framework treats it as a refusal."""


@dataclass(frozen=True)
class Budget:
    """Per-run caps. Every avenue gets one; a bot may tighten or loosen it."""

    max_actions: int = 20
    max_llm_calls: int = 5
    max_seconds: float = 120.0


AVENUE_BUDGETS: dict[Avenue, Budget] = {
    Avenue.MONEY: Budget(max_actions=5, max_llm_calls=3, max_seconds=60),
    Avenue.SECURITY: Budget(max_actions=5, max_llm_calls=5, max_seconds=120),
    Avenue.COMMS: Budget(max_actions=10, max_llm_calls=5, max_seconds=60),
    Avenue.SYSTEM: Budget(max_actions=20, max_llm_calls=2, max_seconds=120),
    Avenue.KNOWLEDGE: Budget(max_actions=50, max_llm_calls=20, max_seconds=300),
    Avenue.GENERAL: Budget(max_actions=100, max_llm_calls=10, max_seconds=300),
}


@dataclass
class Action:
    """A proposed side-effect. By convention bots do not act directly: they return
    Actions and the runner decides whether each one is performed (approval, dedup, budget,
    dry-run, kill switch)."""

    kind: str
    payload: dict = field(default_factory=dict)
    key: Optional[str] = None
    perform: Optional[Callable[["Action"], Any]] = None
    description: str = ""
    bot: str = ""  # filled in by the runner
    status: str = "proposed"
    detail: str = ""

    @property
    def dedup_key(self) -> str:
        if self.key:
            return self.key
        return json.dumps(self.payload, sort_keys=True, default=str)

    @property
    def id(self) -> str:
        """Deterministic id: re-proposing the same action maps to the same id,
        which is what makes approvals and dedup idempotent."""
        raw = f"{self.bot}|{self.kind}|{self.dedup_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "description": self.description,
            "payload": self.payload,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Result:
    """What one work unit produced. `produced` is the canary: zero is a failure
    unless the bot declared that empty runs are expected."""

    produced: int = 0
    artifact: Optional[dict] = None
    actions: list = field(default_factory=list)

    def validate(self) -> None:
        if isinstance(self.produced, bool) or not isinstance(self.produced, int) or self.produced < 0:
            raise TypeError(f"Result.produced must be an int >= 0, got {self.produced!r}")
        if self.artifact is not None and not isinstance(self.artifact, dict):
            raise TypeError("Result.artifact must be a dict or None")
        for a in self.actions:
            if not isinstance(a, Action):
                raise TypeError(f"Result.actions must contain Action objects, got {type(a).__name__}")


@dataclass
class Canary:
    """A known-answer probe. The bot supplies the input and the expected answer;
    the FRAMEWORK runs the work and grades it. A bot never grades itself."""

    input: dict = field(default_factory=dict)
    expect_produced: Optional[int] = None
    expect_artifact: Optional[dict] = None  # subset match
    expect_action_kinds: Optional[list] = None  # multiset match

    def has_expectations(self) -> bool:
        return any(
            x is not None for x in (self.expect_produced, self.expect_artifact, self.expect_action_kinds)
        )


class _DefaultSink:
    """Sentinel meaning 'use the runner's JSONL failure sink'."""

    def __repr__(self) -> str:
        return "DEFAULT_SINK"


DEFAULT_SINK = _DefaultSink()

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def validate_name(name: object) -> str:
    """Bot names become file names (kill switches, caches): only a safe charset."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise NanobotDefinitionError(
            f"invalid name {name!r}: use 1-64 chars of a-z, 0-9, '_', '-', '.' (starting with a-z or 0-9)"
        )
    return name


class Nanobot:
    """Subclass and implement work(ctx) (and canary() to be promotable).

    class Tidy(Nanobot):
        name = "tidy"
        cadence = 3600
        avenue = Avenue.GENERAL
        def work(self, ctx): ...
    """

    name: str = ""
    cadence: float = 0
    avenue: Union[Avenue, str] = Avenue.GENERAL
    expected_empty_ok: Union[bool, int] = False
    budget: Optional[Budget] = None
    failure_sink: Any = DEFAULT_SINK
    dry_run: bool = False
    fail_mode: Optional[str] = None  # "open" | "closed"; default comes from the avenue
    dedup_window_s: float = 24 * 3600
    rate_limit_per_hour: int = 20
    description: str = ""

    def work(self, ctx) -> Result:  # pragma: no cover - abstract
        raise NotImplementedError

    def canary(self) -> Optional[Canary]:
        return None

    # -- derived posture -------------------------------------------------
    @property
    def avenue_enum(self) -> Avenue:
        return Avenue.parse(self.avenue)

    @property
    def effective_budget(self) -> Budget:
        return self.budget or AVENUE_BUDGETS[self.avenue_enum]

    @property
    def fails_closed(self) -> bool:
        if self.fail_mode is None:
            return self.avenue_enum in FAIL_CLOSED_AVENUES
        return self.fail_mode == "closed"

    @property
    def needs_approval(self) -> bool:
        return self.avenue_enum in APPROVAL_AVENUES

    def validate(self) -> None:
        """Raise NanobotDefinitionError if this bot may not be registered."""
        validate_name(self.name)
        if hasattr(self, "approver"):
            raise NanobotDefinitionError(
                f"{self.name}: a bot cannot carry its own approver; approval policy belongs to the Runner"
            )
        if isinstance(self.cadence, bool) or not isinstance(self.cadence, (int, float)) or self.cadence <= 0:
            raise NanobotDefinitionError(f"{self.name}: cadence must be a positive number of seconds")
        avenue = Avenue.parse(self.avenue)
        if self.failure_sink is None:
            raise NanobotDefinitionError(
                f"{self.name}: a nanobot without a failure path cannot be registered "
                "(leave failure_sink as DEFAULT_SINK or pass a sink)"
            )
        if self.failure_sink is not DEFAULT_SINK and not callable(getattr(self.failure_sink, "emit", None)):
            raise NanobotDefinitionError(f"{self.name}: failure_sink must have an emit(record) method")
        if self.fail_mode not in (None, "open", "closed"):
            raise NanobotDefinitionError(f"{self.name}: fail_mode must be 'open', 'closed' or None")
        if self.fail_mode == "open" and avenue in LOCKED_CLOSED_AVENUES:
            raise NanobotDefinitionError(f"{self.name}: {avenue.value} bots are always fail-closed")
        eo = self.expected_empty_ok
        if not isinstance(eo, bool) and (not isinstance(eo, int) or eo < 0):
            raise NanobotDefinitionError(f"{self.name}: expected_empty_ok must be a bool or an int >= 0")
        if not callable(getattr(self, "work", None)):
            raise NanobotDefinitionError(f"{self.name}: work(ctx) is required")

    def __repr__(self) -> str:
        return f"<Nanobot {self.name} avenue={Avenue.parse(self.avenue).value} cadence={self.cadence}s>"


class FunctionNanobot(Nanobot):
    """A nanobot built from a plain function by the @nanobot decorator."""

    def __init__(self, fn: Callable, **attrs: Any) -> None:
        self._fn = fn
        self._canary_fn: Optional[Callable[[], Optional[Canary]]] = attrs.pop("canary", None)
        for k, v in attrs.items():
            if not hasattr(Nanobot, k):
                raise NanobotDefinitionError(f"unknown nanobot option {k!r}")
            setattr(self, k, v)
        self.__doc__ = fn.__doc__

    def work(self, ctx) -> Result:
        return self._fn(ctx)

    def canary(self) -> Optional[Canary]:
        return self._canary_fn() if self._canary_fn else None

    def canary_probe(self, fn: Callable[[], Optional[Canary]]) -> Callable[[], Optional[Canary]]:
        """Decorator: attach the known-answer probe to a function nanobot."""
        self._canary_fn = fn
        return fn

    def __call__(self, ctx) -> Result:
        return self._fn(ctx)


def nanobot(
    name: Optional[str] = None,
    *,
    cadence: float,
    avenue: Union[Avenue, str] = Avenue.GENERAL,
    **options: Any,
) -> Callable[[Callable], FunctionNanobot]:
    """Decorator form:

    @nanobot(name="greeter", cadence=60, avenue="GENERAL")
    def greeter(ctx):
        return Result(produced=1, artifact={"hello": "world"})
    """

    def wrap(fn: Callable) -> FunctionNanobot:
        bot = FunctionNanobot(
            fn,
            name=name or fn.__name__.replace("_", "-"),
            cadence=cadence,
            avenue=Avenue.parse(avenue),
            **options,
        )
        bot.validate()
        return bot

    return wrap
