from conftest import make_bot
from ultimate_nanobot import Action, Canary, Result, Runner


def _payer(performed, avenue="MONEY", **attrs):
    def work(ctx):
        return Result(produced=1, artifact={"ok": True},
                      actions=[Action("payment", key="inv-1", payload={"amount": 5}, perform=performed.append)])

    bot = make_bot(work, avenue=avenue,
                   canary=lambda: Canary(input={}, expect_action_kinds=["payment"]), **attrs)
    return bot


def test_money_action_is_pending_by_default(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    rep = runner.run_once(bot)
    assert rep.ok
    assert rep.actions[0].status == "pending_approval"
    assert performed == []
    assert [p["action_id"] for p in runner.store.pending_approvals()] == [rep.actions[0].id]


def test_approve_then_perform_exactly_once(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    action_id = runner.run_once(bot).actions[0].id
    runner.approve(action_id, by="tester")
    assert runner.run_once(bot).actions[0].status == "performed"
    assert len(performed) == 1
    assert runner.run_once(bot).actions[0].status == "already_executed"
    assert len(performed) == 1  # idempotent: never pays twice


def test_denied_action_is_never_performed(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    action_id = runner.run_once(bot).actions[0].id
    runner.deny(action_id)
    assert runner.run_once(bot).actions[0].status == "denied"
    assert performed == []


def test_callable_approver(tmp_path):
    performed = []
    seen = []
    with Runner(tmp_path / "h", approver=lambda a: seen.append(a.kind) or True) as runner:
        bot = _payer(performed)
        runner.promote(bot)
        assert runner.run_once(bot).actions[0].status == "performed"
    assert seen == ["payment"] and len(performed) == 1


def test_approver_that_raises_counts_as_denial(tmp_path):
    performed = []

    def flaky(action):
        raise TimeoutError("approval service down")

    with Runner(tmp_path / "h", approver=flaky) as runner:
        bot = _payer(performed)
        runner.promote(bot)
        rep = runner.run_once(bot)
    assert rep.actions[0].status == "denied" and performed == []


def test_security_actions_also_need_approval(runner):
    performed = []
    bot = _payer(performed, avenue="SECURITY")
    runner.promote(bot)
    assert runner.run_once(bot).actions[0].status == "pending_approval"
    assert performed == []


def test_general_actions_do_not_need_approval(runner):
    performed = []
    bot = _payer(performed, avenue="GENERAL")
    runner.promote(bot)
    assert runner.run_once(bot).actions[0].status == "performed"
    assert runner.store.pending_approvals() == []


def test_dry_run_default_and_live_override(runner):
    performed = []
    bot = _payer(performed, avenue="GENERAL", dry_run=True)
    runner.promote(bot)
    assert runner.run_once(bot).actions[0].status == "dry_run"
    assert performed == []
    assert runner.run_once(bot, live=True).actions[0].status == "performed"
