import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ultimate_nanobot import Canary, Nanobot, Result, Runner, StoreUnavailable  # noqa: E402
from ultimate_nanobot.store import SQLiteStore  # noqa: E402


@pytest.fixture
def runner(tmp_path):
    r = Runner(tmp_path / "home")
    yield r
    r.close()


def make_bot(work, name="test-bot", avenue="GENERAL", cadence=60, canary=None, **attrs):
    """Build a Nanobot subclass instance from a work function."""

    def _canary(self):
        return canary() if callable(canary) else canary

    cls = type(
        "TestBot",
        (Nanobot,),
        {"name": name, "avenue": avenue, "cadence": cadence, "work": lambda self, ctx: work(ctx),
         "canary": _canary, **attrs},
    )
    return cls()


def one(ctx):
    return Result(produced=1, artifact={"n": 1})


def good_canary():
    return Canary(input={}, expect_produced=1, expect_artifact={"n": 1})


def enable(runner, bot):
    rep = runner.promote(bot)
    assert rep.passed, rep.detail
    return bot


class BrokenStore(SQLiteStore):
    """A store whose backend is 'down': every call raises StoreUnavailable."""

    def __init__(self):  # no connection at all
        pass

    def _exec(self, *a, **k):
        raise StoreUnavailable("simulated outage")

    def close(self):
        pass


class ToggleStore(SQLiteStore):
    """A real SQLite store that can be switched 'down' mid-test."""

    down = False

    def _exec(self, *a, **k):
        if self.down:
            raise StoreUnavailable("simulated outage")
        return super()._exec(*a, **k)
