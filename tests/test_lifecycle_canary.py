from conftest import good_canary, make_bot, one
from ultimate_nanobot import Action, Canary, Lifecycle, Result, SkipCanary


def test_new_bot_is_discovered_and_does_not_run(runner):
    ran = []
    bot = make_bot(lambda ctx: ran.append(1) or Result(produced=1))
    rep = runner.run_once(bot)
    assert rep.verdict == "not_enabled" and rep.exit_code == 3
    assert ran == []
    assert runner.store.get_lifecycle(bot.name) is Lifecycle.DISCOVERED


def test_passing_canary_promotes_to_enabled(runner):
    bot = make_bot(one, canary=good_canary)
    rep = runner.promote(bot)
    assert rep.passed
    assert runner.store.get_lifecycle(bot.name) is Lifecycle.ENABLED
    states = [t["to_state"] for t in runner.store.transitions(bot.name)]
    assert states == ["DISCOVERED", "CANDIDATE", "ENABLED"]


def test_missing_canary_is_a_refusal(runner):
    bot = make_bot(one, canary=None)
    rep = runner.promote(bot)
    assert not rep.passed and "no canary" in rep.detail
    assert runner.store.get_lifecycle(bot.name) is Lifecycle.CANDIDATE
    assert runner.failure_sink.read()[-1]["kind"] == "canary_refused"


def test_skipped_canary_is_a_refusal(runner):
    def skip():
        raise SkipCanary("network not available")

    rep = runner.promote(make_bot(one, canary=skip))
    assert not rep.passed and "refused" in rep.detail
    assert runner.store.get_lifecycle("test-bot") is Lifecycle.CANDIDATE


def test_canary_without_expectations_is_a_refusal(runner):
    rep = runner.promote(make_bot(one, canary=lambda: Canary(input={})))
    assert not rep.passed and "no expected answer" in rep.detail


def test_wrong_answer_fails_the_canary(runner):
    rep = runner.promote(make_bot(one, canary=lambda: Canary(input={}, expect_artifact={"n": 2})))
    assert not rep.passed and "expected 2, got 1" in rep.detail
    assert runner.store.get_lifecycle("test-bot") is Lifecycle.CANDIDATE


def test_framework_grades_not_the_bot(runner):
    # a bot that claims success in its artifact but produces nothing is still refused
    bot = make_bot(lambda ctx: Result(produced=0, artifact={"self_test": "PASS"}),
                   canary=lambda: Canary(input={}, expect_artifact={"self_test": "PASS"}))
    rep = runner.promote(bot)
    assert not rep.passed and "produced 0" in rep.detail


def test_canary_never_performs_actions(runner):
    performed = []

    def work(ctx):
        return Result(produced=1, artifact={"n": 1}, actions=[Action("send", perform=performed.append)])

    rep = runner.run_canary(make_bot(work, canary=lambda: Canary(input={}, expect_action_kinds=["send"])))
    assert rep.passed
    assert performed == []
    assert rep.report.actions[0].status == "not_performed"


def test_disabled_bot_does_not_run_and_needs_canary_to_return(runner):
    bot = make_bot(one, canary=good_canary)
    runner.promote(bot)
    runner.disable(bot.name)
    assert runner.run_once(bot).verdict == "not_enabled"
    assert runner.promote(bot).passed
    assert runner.run_once(bot).ok


def test_failed_recheck_demotes_an_enabled_bot(runner):
    answer = {"n": 1}
    bot = make_bot(lambda ctx: Result(produced=1, artifact=dict(answer)), canary=good_canary)
    runner.promote(bot)
    answer["n"] = 99  # regression
    assert not runner.promote(bot).passed
    assert runner.store.get_lifecycle(bot.name) is Lifecycle.CANDIDATE
