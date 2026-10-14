"""ultimate-nanobot: tiny, dependency-free autonomous workers with guardrails."""

from .core import (
    AVENUE_BUDGETS,
    DEFAULT_SINK,
    Action,
    Avenue,
    Budget,
    BudgetExceeded,
    Canary,
    FunctionNanobot,
    Lifecycle,
    LLMUnavailable,
    Nanobot,
    NanobotDefinitionError,
    NanobotError,
    Result,
    SkipCanary,
    nanobot,
)
from .ledger import Ledger, rotate_to_archive
from .liveness import fleet_status, verdict
from .runner import CanaryReport, Context, RunReport, Runner, load_bot
from .sinks import CallableSink, FailureSink, JsonlSink, WebhookSink
from .store import SQLiteStore, Store, StoreUnavailable

__version__ = "0.1.0"

__all__ = [
    "AVENUE_BUDGETS",
    "DEFAULT_SINK",
    "Action",
    "Avenue",
    "Budget",
    "BudgetExceeded",
    "CallableSink",
    "Canary",
    "CanaryReport",
    "Context",
    "FailureSink",
    "FunctionNanobot",
    "JsonlSink",
    "LLMUnavailable",
    "Ledger",
    "Lifecycle",
    "Nanobot",
    "NanobotDefinitionError",
    "NanobotError",
    "Result",
    "RunReport",
    "Runner",
    "SQLiteStore",
    "SkipCanary",
    "Store",
    "StoreUnavailable",
    "WebhookSink",
    "fleet_status",
    "load_bot",
    "nanobot",
    "rotate_to_archive",
    "verdict",
    "__version__",
]
