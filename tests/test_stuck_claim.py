"""A run killed between claim and result leaves its approval EXECUTING. That is never retried automatically (the action may have
gone through), but it must be visible and a human must be able to hand it back."""
import pytest
from conftest import make_bot
from ultimate_nanobot import Action, Canary, Result
from ultimate_nanobot.cli import main as cli_main


def _payer(performed):
    def work(ctx):
        return Result(produced=1, artifact={"ok": True},
                      actions=[Action("payment", key="inv-1", payload={"amount": 5}, perform=performed.append)])
    return make_bot(work, avenue="MONEY", canary=lambda: Canary(input={}, expect_action_kinds=["payment"]))


def _crash_after_claim(runner, bot, performed):
    """Approve the action, then claim it the way a runner does, and 'die' before anything is performed or recorded."""
    action_id = runner.run_once(bot).actions[0].id
    runner.approve(action_id, by="tester")
    assert runner.store.claim_approval(action_id, runner.clock()) is True
    return action_id


def test_a_crashed_claim_is_stuck_not_retried_and_listed(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    action_id = _crash_after_claim(runner, bot, performed)
    rep = runner.run_once(bot)
    assert rep.actions[0].status == "already_executing" and performed == [], "never retried: it may have gone through"
    assert [r["action_id"] for r in runner.stuck_approvals(older_than_s=0)] == [action_id]
    assert runner.stuck_approvals(older_than_s=3600) == []
    with pytest.raises(ValueError):
        runner.approve(action_id)   # still not approvable: the operator must release it first
    assert runner.store.pending_approvals() == []


def test_release_hands_a_stale_claim_back_to_a_human(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    action_id = _crash_after_claim(runner, bot, performed)
    row = runner.release(action_id, by="tester", min_age_s=0)
    assert row["status"] == "FAILED" and "released" in row["decided_by"]
    assert runner.run_once(bot).actions[0].status == "needs_reapproval" and performed == []
    runner.approve(action_id, by="tester")
    assert runner.run_once(bot).actions[0].status == "performed" and len(performed) == 1


def test_release_refuses_a_claim_that_may_still_be_running(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    action_id = _crash_after_claim(runner, bot, performed)
    with pytest.raises(ValueError, match="may still be performing"):
        runner.release(action_id)   # claimed seconds ago, default minimum age applies
    assert runner.store.get_approval(action_id)["status"] == "EXECUTING"


def test_release_only_touches_an_executing_claim(runner):
    performed = []
    bot = _payer(performed)
    runner.promote(bot)
    action_id = runner.run_once(bot).actions[0].id          # PENDING
    with pytest.raises(ValueError, match="only an EXECUTING claim"):
        runner.release(action_id, min_age_s=0)
    with pytest.raises(KeyError):
        runner.release("no-such-id", min_age_s=0)


def test_cli_lists_the_stuck_claim_and_releases_it(tmp_path, capsys):
    from ultimate_nanobot import Runner
    home = tmp_path / "home"
    performed = []
    with Runner(home) as runner:
        bot = _payer(performed)
        runner.promote(bot)
        action_id = _crash_after_claim(runner, bot, performed)
    assert cli_main(["--home", str(home), "pending", "--stuck-after", "0"]) == 0
    out = capsys.readouterr().out
    assert "stuck" in out and action_id in out
    assert cli_main(["--home", str(home), "release", action_id]) == 1       # too fresh without --force
    assert cli_main(["--home", str(home), "release", action_id, "--force"]) == 0
    assert "FAILED" in capsys.readouterr().out
