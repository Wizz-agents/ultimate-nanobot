"""Rule: produced, not ran. Rule: heartbeat only after a completed work unit."""

from conftest import enable, good_canary, make_bot
from ultimate_nanobot import Canary, Result


def test_zero_output_is_a_failure_by_default(runner):
    calls = {"n": 0}

    def work(ctx):
        calls["n"] += 1
        return Result(produced=1 if ctx.canary else 0, artifact={"n": 1})

    bot = enable(runner, make_bot(work, canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.verdict == "zero_output"
    assert not rep.ok and rep.exit_code == 1
    failures = runner.failure_sink.read()
    assert failures[-1]["kind"] == "zero_output"


def test_expected_empty_ok_true_allows_empty_runs(runner):
    bot = enable(runner, make_bot(lambda ctx: Result(produced=1 if ctx.canary else 0, artifact={"n": 1}),
                                  canary=good_canary, expected_empty_ok=True))
    rep = runner.run_once(bot)
    assert rep.verdict == "ok_empty" and rep.ok and rep.heartbeat


def test_empty_streak_allows_n_then_fails_and_resets(runner):
    state = {"produce": 0}

    def work(ctx):
        return Result(produced=1 if ctx.canary else state["produce"], artifact={"n": 1})

    bot = enable(runner, make_bot(work, canary=good_canary, expected_empty_ok=2))
    assert runner.run_once(bot).verdict == "ok_empty"
    assert runner.run_once(bot).verdict == "ok_empty"
    assert runner.run_once(bot).verdict == "zero_output"  # third empty in a row
    state["produce"] = 1
    assert runner.run_once(bot).verdict == "ok"  # streak resets
    state["produce"] = 0
    assert runner.run_once(bot).verdict == "ok_empty"


def test_non_result_return_is_a_failure(runner):
    def work(ctx):
        return Result(produced=1, artifact={"n": 1}) if ctx.canary else {"produced": 5}

    bot = enable(runner, make_bot(work, canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.verdict == "bad_result"
    assert runner.store.heartbeats(bot.name) == []


def test_heartbeat_written_after_success(runner):
    bot = enable(runner, make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.ok and rep.heartbeat
    assert len(runner.store.heartbeats(bot.name)) == 1


def test_no_heartbeat_on_exception(runner):
    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        raise RuntimeError("boom")

    bot = enable(runner, make_bot(work, canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.verdict == "error" and "boom" in rep.error
    assert runner.store.heartbeats(bot.name) == []


def test_no_heartbeat_on_zero_output(runner):
    bot = enable(runner, make_bot(lambda ctx: Result(produced=1 if ctx.canary else 0, artifact={"n": 1}),
                                  canary=good_canary))
    runner.run_once(bot)
    assert runner.store.heartbeats(bot.name) == []


def test_heartbeat_is_not_written_at_the_start_of_a_run(runner):
    seen = {}

    def work(ctx):
        if not ctx.canary:
            seen["during"] = list(runner.store.heartbeats("test-bot"))
        return Result(produced=1, artifact={"n": 1})

    bot = enable(runner, make_bot(work, canary=good_canary))
    runner.run_once(bot)
    assert seen["during"] == []
    assert len(runner.store.heartbeats(bot.name)) == 1


def test_shadow_run_earns_no_heartbeat_and_performs_nothing(runner):
    from ultimate_nanobot import Action

    performed = []

    def work(ctx):
        return Result(produced=1, artifact={"n": 1}, actions=[Action("x", perform=performed.append)])

    bot = make_bot(work, canary=lambda: Canary(input={}, expect_produced=1))
    rep = runner.run_once(bot, shadow=True)  # not enabled, still allowed in shadow
    assert rep.ok and not rep.heartbeat
    assert performed == []
    assert rep.actions[0].status == "not_performed"
