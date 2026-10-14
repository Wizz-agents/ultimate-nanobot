import time

from conftest import enable, good_canary, make_bot
from ultimate_nanobot import Action, Budget, Canary, LLMUnavailable, Result, Runner


def _counting_bot(ran, **attrs):
    def work(ctx):
        if not ctx.canary:
            ran.append(1)
        return Result(produced=1, artifact={"n": 1})

    return make_bot(work, canary=good_canary, **attrs)


# --- kill switch ---------------------------------------------------------
def test_global_kill_file_stops_every_bot(runner):
    ran = []
    bot = enable(runner, _counting_bot(ran))
    runner.kill()
    rep = runner.run_once(bot)
    assert rep.verdict == "killed" and ran == []
    runner.revive()
    assert runner.run_once(bot).ok and ran == [1]


def test_per_bot_kill_file(runner):
    ran = []
    bot = enable(runner, _counting_bot(ran))
    other = enable(runner, _counting_bot(ran, name="other-bot"))
    runner.kill(bot.name)
    assert runner.run_once(bot).verdict == "killed"
    assert runner.run_once(other).ok
    assert ran == [1]


def test_store_kill_flag(runner):
    ran = []
    bot = enable(runner, _counting_bot(ran))
    runner.store.set_flag(f"kill:{bot.name}", "maintenance")
    assert runner.run_once(bot).verdict == "killed"
    runner.store.set_flag(f"kill:{bot.name}", None)
    runner.store.set_flag("kill:all", "incident")
    assert runner.run_once(bot).verdict == "killed"
    assert ran == []


def test_kill_file_works_even_when_store_is_down(tmp_path):
    from conftest import BrokenStore

    runner = Runner(tmp_path / "h", store=BrokenStore())
    runner.kill()
    rep = runner.run_once(make_bot(lambda ctx: Result(produced=1), avenue="SYSTEM"))
    assert rep.verdict == "killed"


# --- budgets -------------------------------------------------------------
def test_too_many_actions_refuses_the_whole_batch(runner):
    performed = []

    def work(ctx):
        n = 1 if ctx.canary else 5
        return Result(produced=n, artifact={"n": 1},
                      actions=[Action("touch", key=str(i), perform=performed.append) for i in range(n)])

    bot = enable(runner, make_bot(work, canary=lambda: Canary(input={}, expect_produced=1),
                                  budget=Budget(max_actions=3, max_llm_calls=0, max_seconds=10)))
    rep = runner.run_once(bot)
    assert rep.verdict == "budget_exceeded"
    assert performed == []
    assert {a.status for a in rep.actions} == {"not_performed"}


def test_llm_calls_are_counted_and_capped(tmp_path):
    prompts = []

    def work(ctx):
        for i in range(3):
            ctx.llm(f"q{i}")
        return Result(produced=1, artifact={"n": 1})

    with Runner(tmp_path / "h", llm=lambda p: prompts.append(p) or "ok") as runner:
        bot = make_bot(work, canary=good_canary, budget=Budget(max_actions=5, max_llm_calls=2, max_seconds=10))
        rep = runner.run_canary(bot)
        assert not rep.passed  # the canary itself hits the cap
        runner.store.register(bot.name, "GENERAL", 60)
        from ultimate_nanobot import Lifecycle
        runner.store.set_lifecycle(bot.name, Lifecycle.ENABLED, "test")
        rep = runner.run_once(bot)
        assert rep.verdict == "budget_exceeded"
        assert runner.ledger.records()[-1]["llm_calls"] == 2


def test_llm_is_optional_and_routes_to_user_callable(tmp_path):
    def work(ctx):
        if not ctx.has_llm:
            try:
                ctx.llm("hi")
            except LLMUnavailable:
                return Result(produced=1, artifact={"n": 1, "llm": "none"})
        return Result(produced=1, artifact={"n": 1, "llm": ctx.llm("hi")})

    with Runner(tmp_path / "a") as plain:
        bot = enable(plain, make_bot(work, canary=good_canary))
        assert plain.run_once(bot).artifact["llm"] == "none"
    with Runner(tmp_path / "b", llm=lambda p: p.upper()) as smart:
        bot = enable(smart, make_bot(work, canary=good_canary))
        rep = smart.run_once(bot)
        assert rep.artifact["llm"] == "HI"
        assert smart.ledger.records()[-1]["llm_calls"] == 1


def test_over_time_run_performs_no_actions(runner):
    performed = []

    def work(ctx):
        if not ctx.canary:
            time.sleep(0.3)
        return Result(produced=1, artifact={"n": 1}, actions=[Action("x", perform=performed.append)])

    bot = enable(runner, make_bot(work, canary=good_canary,
                                  budget=Budget(max_actions=5, max_llm_calls=0, max_seconds=0.1)))
    rep = runner.run_once(bot)
    assert rep.verdict == "over_time"
    assert performed == []


def test_check_deadline_raises_inside_long_work(runner):
    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        while True:
            ctx.check_deadline()
            time.sleep(0.02)

    bot = enable(runner, make_bot(work, canary=good_canary,
                                  budget=Budget(max_actions=5, max_llm_calls=0, max_seconds=0.1)))
    assert runner.run_once(bot).verdict == "budget_exceeded"


# --- COMMS ---------------------------------------------------------------
def _messenger(sent, messages, **attrs):
    def work(ctx):
        msgs = [{"to": "ops", "text": "canary"}] if ctx.canary else messages()
        return Result(produced=len(msgs), artifact={"n": len(msgs)},
                      actions=[Action("message", payload=m, perform=sent.append) for m in msgs])

    return make_bot(work, avenue="COMMS", canary=lambda: Canary(input={}, expect_produced=1), **attrs)


def test_comms_dedups_identical_messages(runner):
    sent = []
    bot = enable(runner, _messenger(sent, lambda: [{"to": "ops", "text": "disk 91%"}]))
    assert runner.run_once(bot).actions[0].status == "performed"
    assert runner.run_once(bot).actions[0].status == "deduped"
    assert len(sent) == 1


def test_comms_dedup_window_expires(tmp_path):
    clock = {"t": 1_000_000.0}
    sent = []
    with Runner(tmp_path / "h", clock=lambda: clock["t"]) as runner:
        bot = enable(runner, _messenger(sent, lambda: [{"to": "ops", "text": "same"}], dedup_window_s=60))
        runner.run_once(bot)
        clock["t"] += 61
        assert runner.run_once(bot).actions[0].status == "performed"
    assert len(sent) == 2


def test_comms_rate_limit(runner):
    sent = []
    counter = iter(range(1000))
    bot = enable(runner, _messenger(sent, lambda: [{"to": "ops", "text": f"m{next(counter)}"} for _ in range(3)],
                                    rate_limit_per_hour=4))
    first = runner.run_once(bot)
    second = runner.run_once(bot)
    statuses = [a.status for a in first.actions + second.actions]
    assert statuses.count("performed") == 4
    assert statuses.count("rate_limited") == 2
    assert len(sent) == 4
