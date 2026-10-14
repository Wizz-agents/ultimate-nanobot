import pytest

from conftest import make_bot, one
from ultimate_nanobot import (
    AVENUE_BUDGETS,
    Avenue,
    Budget,
    CallableSink,
    Nanobot,
    NanobotDefinitionError,
    Result,
    nanobot,
)


def test_decorator_builds_a_valid_bot():
    @nanobot(name="greeter", cadence=30, avenue="knowledge")
    def greeter(ctx):
        return Result(produced=1)

    assert greeter.name == "greeter"
    assert greeter.cadence == 30
    assert greeter.avenue_enum is Avenue.KNOWLEDGE
    assert greeter.effective_budget == AVENUE_BUDGETS[Avenue.KNOWLEDGE]


def test_decorator_default_name_and_canary_probe():
    @nanobot(cadence=10)
    def tidy_desk(ctx):
        return Result(produced=1)

    @tidy_desk.canary_probe
    def _probe():
        return None

    assert tidy_desk.name == "tidy-desk"
    assert tidy_desk.canary() is None


def test_subclass_bot_validates():
    class Tidy(Nanobot):
        name = "tidy"
        cadence = 5
        avenue = Avenue.SYSTEM

        def work(self, ctx):
            return Result(produced=1)

    Tidy().validate()


def test_unknown_avenue_rejected():
    with pytest.raises(NanobotDefinitionError):
        nanobot(name="x", cadence=1, avenue="CASINO")(lambda ctx: Result(1))


@pytest.mark.parametrize("cadence", [0, -5, "60", True])
def test_bad_cadence_rejected(cadence):
    with pytest.raises(NanobotDefinitionError):
        make_bot(one, cadence=cadence).validate()


def test_bad_name_rejected():
    with pytest.raises(NanobotDefinitionError):
        make_bot(one, name="Has Spaces!").validate()


def test_bot_without_failure_path_cannot_be_registered(runner):
    bot = make_bot(one, failure_sink=None)
    with pytest.raises(NanobotDefinitionError, match="failure path"):
        runner.register(bot)


def test_sink_without_emit_rejected(runner):
    bot = make_bot(one, failure_sink=object())
    with pytest.raises(NanobotDefinitionError):
        runner.register(bot)


def test_custom_sink_accepted(runner):
    bot = make_bot(one, failure_sink=CallableSink(lambda r: None))
    runner.register(bot)


@pytest.mark.parametrize("avenue", ["MONEY", "SECURITY"])
def test_money_and_security_cannot_be_made_fail_open(avenue):
    with pytest.raises(NanobotDefinitionError, match="fail-closed"):
        make_bot(one, avenue=avenue, fail_mode="open").validate()


def test_every_avenue_has_a_budget_and_bot_can_override():
    for avenue in Avenue:
        assert isinstance(AVENUE_BUDGETS[avenue], Budget)
    custom = Budget(max_actions=1, max_llm_calls=0, max_seconds=1)
    assert make_bot(one, budget=custom).effective_budget == custom


def test_unknown_decorator_option_rejected():
    with pytest.raises(NanobotDefinitionError):
        nanobot(name="x", cadence=1, colour="blue")(lambda ctx: Result(1))
