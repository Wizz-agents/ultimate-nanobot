import pytest

from conftest import enable, good_canary, make_bot
from ultimate_nanobot import Result, Runner, fleet_status, verdict


@pytest.mark.parametrize(
    "age, expected",
    [(0, "OK"), (89, "OK"), (90, "OK"), (91, "LATE"), (180, "LATE"), (181, "DARK"), (10_000, "DARK")],
)
def test_cadence_relative_verdicts(age, expected):
    now = 1_000_000.0
    assert verdict(now - age, cadence=60, now=now) == expected


def test_never_when_no_heartbeat():
    assert verdict(None, cadence=60) == "NEVER"


def test_same_age_different_cadence():
    now = 1_000_000.0
    two_hours_ago = now - 7200
    assert verdict(two_hours_ago, cadence=86400, now=now) == "OK"
    assert verdict(two_hours_ago, cadence=60, now=now) == "DARK"


def test_fleet_status_reports_liveness_from_heartbeat_history(tmp_path):
    clock = {"t": 1_000_000.0}
    runner = Runner(tmp_path / "h", clock=lambda: clock["t"])
    try:
        bot = enable(runner, make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), canary=good_canary))
        idle = make_bot(lambda ctx: Result(produced=1), name="idle-bot")
        runner.register(idle)
        runner.run_once(bot)
        rows = {r["name"]: r for r in fleet_status(runner.store, now=clock["t"] + 30)}
        assert rows["test-bot"]["liveness"] == "OK"
        assert rows["test-bot"]["lifecycle"] == "ENABLED"
        assert rows["idle-bot"]["liveness"] == "NEVER"
        rows = {r["name"]: r for r in fleet_status(runner.store, now=clock["t"] + 120)}
        assert rows["test-bot"]["liveness"] == "LATE"
        rows = {r["name"]: r for r in fleet_status(runner.store, now=clock["t"] + 1000)}
        assert rows["test-bot"]["liveness"] == "DARK"
    finally:
        runner.close()


def test_failing_bot_goes_dark_because_errors_never_beat(tmp_path):
    clock = {"t": 1_000_000.0}
    runner = Runner(tmp_path / "h", clock=lambda: clock["t"])
    state = {"fail": False}

    def work(ctx):
        if state["fail"] and not ctx.canary:
            raise RuntimeError("down")
        return Result(produced=1, artifact={"n": 1})

    try:
        bot = enable(runner, make_bot(work, canary=good_canary))
        runner.run_once(bot)
        state["fail"] = True
        for _ in range(5):
            clock["t"] += 60
            runner.run_once(bot)
        row = fleet_status(runner.store, now=clock["t"])[0]
        assert row["liveness"] == "DARK"
        assert row["last_verdict"] == "error"
    finally:
        runner.close()
