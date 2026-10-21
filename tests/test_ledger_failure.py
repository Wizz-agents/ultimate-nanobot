"""A failed ledger write is bookkeeping: it must be escalated, never raised out of a run whose actions already happened."""
import pytest
from conftest import enable, good_canary, make_bot
from ultimate_nanobot import Action, CallableSink, Result


def _acting(performed):
    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        return Result(produced=1, artifact={"n": 1}, actions=[Action(kind="note", payload={}, perform=lambda a: performed.append(1))])
    return work


def _break_ledger(runner, monkeypatch):
    def boom(_record):
        raise OSError("disk full")
    monkeypatch.setattr(runner.ledger, "append", boom)


def test_a_ledger_failure_after_the_actions_is_escalated_not_raised(runner, monkeypatch):
    performed = []
    bot = enable(runner, make_bot(_acting(performed), canary=good_canary, dry_run=False))
    _break_ledger(runner, monkeypatch)
    rep = runner.run_once(bot, live=True)
    assert rep.verdict == "ok" and rep.exit_code == 0, "the work happened; the verdict is the work's"
    assert performed == [1] and [a.status for a in rep.actions] == ["performed"]
    assert len(rep.bookkeeping_errors) == 1 and "disk full" in rep.bookkeeping_errors[0]
    record = runner.failure_sink.read()[-1]
    assert record["kind"] == "ledger_write_failed" and record["run_id"] == rep.run_id


def test_the_run_is_still_recorded_in_the_store(runner, monkeypatch):
    bot = enable(runner, make_bot(_acting([]), canary=good_canary, dry_run=False))
    _break_ledger(runner, monkeypatch)
    rep = runner.run_once(bot, live=True)
    last = runner.store.recent_runs(bot.name, limit=1)
    assert last and last[0]["verdict"] == "ok" and rep.heartbeat is True


def test_when_the_failure_path_is_down_too_the_run_still_returns(runner, monkeypatch, capsys):
    def dead(_record):
        raise RuntimeError("sink down")
    bot = enable(runner, make_bot(_acting([]), canary=good_canary, dry_run=False, failure_sink=CallableSink(dead)))
    runner.fallback_sink = CallableSink(dead)
    _break_ledger(runner, monkeypatch)
    rep = runner.run_once(bot, live=True)
    assert rep.verdict == "ok" and len(rep.bookkeeping_errors) == 1
    assert "ledger write failed" in capsys.readouterr().err


def test_a_healthy_ledger_leaves_no_bookkeeping_errors(runner):
    bot = enable(runner, make_bot(_acting([]), canary=good_canary, dry_run=False))
    rep = runner.run_once(bot, live=True)
    assert rep.bookkeeping_errors == []


@pytest.mark.parametrize("mode", ["dry", "shadow"])
def test_dry_and_shadow_runs_survive_a_ledger_failure_too(runner, monkeypatch, mode):
    bot = enable(runner, make_bot(_acting([]), canary=good_canary))
    _break_ledger(runner, monkeypatch)
    rep = runner.run_once(bot, shadow=(mode == "shadow"))
    assert len(rep.bookkeeping_errors) == 1
