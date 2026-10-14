"""Store unreachable: avenue decides fail-open vs fail-closed."""

import pytest

from conftest import BrokenStore, ToggleStore, good_canary, make_bot
from ultimate_nanobot import Action, Result, Runner


@pytest.fixture
def broken_runner(tmp_path):
    r = Runner(tmp_path / "home", store=BrokenStore())
    yield r


@pytest.mark.parametrize("avenue", ["MONEY", "SECURITY", "COMMS"])
def test_fail_closed_avenues_refuse_to_run(broken_runner, avenue):
    ran = []
    bot = make_bot(lambda ctx: ran.append(1) or Result(produced=1), avenue=avenue)
    rep = broken_runner.run_once(bot)
    assert rep.verdict == "refused_store_unavailable"
    assert ran == []
    assert broken_runner.failure_sink.read()[-1]["kind"] == "store_unavailable"
    assert broken_runner.ledger.records()[-1]["verdict"] == "refused_store_unavailable"


@pytest.mark.parametrize("avenue", ["SYSTEM", "KNOWLEDGE", "GENERAL"])
def test_fail_open_avenues_keep_working_degraded(tmp_path, avenue):
    """An ENABLED fail-open bot keeps acting through an outage (lifecycle cache)."""
    store = ToggleStore(tmp_path / "home" / "nanobot.db")
    runner = Runner(tmp_path / "home", store=store)
    done = []
    bot = make_bot(lambda ctx: Result(produced=1, artifact={"n": 1},
                                      actions=[Action("restart", perform=done.append)]),
                   avenue=avenue, canary=good_canary)
    assert runner.promote(bot).passed
    store.down = True
    rep = runner.run_once(bot)
    assert rep.ok and rep.degraded
    assert len(done) == 1
    assert not rep.heartbeat  # could not be written, and the ledger says so
    assert runner.ledger.records()[-1]["degraded"] is True
    store.down = False
    runner.close()


@pytest.mark.parametrize("avenue", ["SYSTEM", "KNOWLEDGE", "GENERAL"])
def test_unknown_lifecycle_in_outage_works_but_never_acts(broken_runner, avenue):
    done = []
    bot = make_bot(lambda ctx: Result(produced=1, actions=[Action("rm", perform=done.append)]), avenue=avenue)
    rep = broken_runner.run_once(bot)  # never promoted, store down, no cache
    assert rep.degraded and done == []
    assert rep.actions[0].status == "not_performed"


def test_comms_can_opt_into_fail_open_but_never_sends_ungated(broken_runner):
    sent = []
    bot = make_bot(lambda ctx: Result(produced=1, actions=[Action("message", perform=sent.append)]),
                   avenue="COMMS", fail_mode="open")
    rep = broken_runner.run_once(bot)
    assert rep.ok
    assert sent == []  # dedup gate unavailable -> not sent
    assert rep.actions[0].status == "not_performed"


def test_fail_closed_bot_with_custom_override(broken_runner):
    bot = make_bot(lambda ctx: Result(produced=1), avenue="SYSTEM", fail_mode="closed")
    assert broken_runner.run_once(bot).verdict == "refused_store_unavailable"
