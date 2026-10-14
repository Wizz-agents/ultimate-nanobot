"""Regression tests for the adversarial review (one or more per finding)."""

import json
import threading
import time
from pathlib import Path

import pytest

from conftest import ROOT, BrokenStore, ToggleStore, enable, good_canary, make_bot
from ultimate_nanobot import (
    Action,
    Canary,
    Ledger,
    Lifecycle,
    NanobotDefinitionError,
    Result,
    Runner,
    nanobot,
)
from ultimate_nanobot import ledger as ledger_mod
from ultimate_nanobot.cli import main


def _payer(perform, key="INV-1", avenue="MONEY"):
    return make_bot(
        lambda ctx: Result(produced=1, artifact={"n": 1},
                           actions=[Action("payment", key=key, payload={"amt": 1}, perform=perform)]),
        name="payout", avenue=avenue, canary=lambda: Canary(input={}, expect_action_kinds=["payment"]),
    )


# --- 1. exactly-once ---------------------------------------------------------
def test_one_approval_two_concurrent_runners_pay_once(tmp_path):
    home = tmp_path / "home"
    paid, lock = [], threading.Lock()

    def slowpay(action):
        time.sleep(0.3)
        with lock:
            paid.append(action.id)

    bot = _payer(slowpay)
    with Runner(home) as r:
        r.promote(bot)
        r.approve(r.run_once(bot).actions[0].id)
    runners = [Runner(home), Runner(home)]
    reports = []
    threads = [threading.Thread(target=lambda rr=rr: reports.append(rr.run_once(bot))) for rr in runners]
    [t.start() for t in threads]
    [t.join() for t in threads]
    [rr.close() for rr in runners]
    assert len(paid) == 1
    assert sorted(rep.actions[0].status for rep in reports) == ["already_executing", "performed"]


def test_failed_perform_needs_reapproval_and_is_not_retried(runner):
    calls = []

    def flaky(action):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("gateway timed out (payment may have gone through)")

    bot = _payer(flaky)
    runner.promote(bot)
    aid = runner.run_once(bot).actions[0].id
    runner.approve(aid)
    assert runner.run_once(bot).verdict == "action_failed"
    for _ in range(3):
        assert runner.run_once(bot).actions[0].status == "needs_reapproval"
    assert len(calls) == 1
    assert runner.store.get_approval(aid)["status"] == "FAILED"
    runner.approve(aid)  # a human decides to retry
    assert runner.run_once(bot).actions[0].status == "performed"
    assert len(calls) == 2


def test_executed_action_cannot_be_reapproved_or_denied(runner):
    bot = _payer(lambda a: None)
    runner.promote(bot)
    aid = runner.run_once(bot).actions[0].id
    runner.approve(aid)
    runner.run_once(bot)
    with pytest.raises(ValueError):
        runner.approve(aid)
    with pytest.raises(ValueError):
        runner.deny(aid)


# --- 2. no self-approval --------------------------------------------------------
def test_bot_with_its_own_approver_cannot_be_registered(runner):
    bot = make_bot(lambda ctx: Result(produced=1), avenue="MONEY", approver=staticmethod(lambda a: True))
    with pytest.raises(NanobotDefinitionError, match="approver"):
        runner.run_once(bot)
    with pytest.raises(NanobotDefinitionError):
        nanobot(name="x", cadence=1, avenue="MONEY", approver=lambda a: True)(lambda ctx: Result(1))


def test_money_default_is_deny_without_runner_policy(runner):
    paid = []
    bot = _payer(paid.append)
    runner.promote(bot)
    for _ in range(3):
        assert runner.run_once(bot).actions[0].status == "pending_approval"
    assert paid == []


# --- 3. degraded mode respects the last confirmed lifecycle ----------------------
def test_disabled_bot_stays_off_during_outage(tmp_path):
    store = ToggleStore(tmp_path / "h" / "nanobot.db")
    runner = Runner(tmp_path / "h", store=store)
    done = []
    bot = make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}, actions=[Action("x", perform=done.append)]),
                   avenue="SYSTEM", canary=good_canary)
    runner.promote(bot)
    runner.disable(bot.name)
    store.down = True
    assert runner.run_once(bot).verdict == "not_enabled"
    assert done == []
    store.down = False
    runner.close()


def test_avenue_change_is_not_trusted_from_cache_during_outage(tmp_path):
    store = ToggleStore(tmp_path / "h" / "nanobot.db")
    runner = Runner(tmp_path / "h", store=store)
    bot = make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), avenue="SYSTEM", canary=good_canary)
    runner.promote(bot)
    store.down = True
    swapped = make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), avenue="GENERAL")
    assert runner.run_once(swapped).verdict == "not_enabled"
    store.down = False
    runner.close()


# --- 4. names never become paths ---------------------------------------------------
@pytest.mark.parametrize("name", ["../../../ledger.jsonl", "..", "a/b", "a\\b", "/abs", "C:x"])
def test_kill_and_revive_reject_path_names(runner, name):
    runner.ledger.append({"keep": True})
    with pytest.raises(NanobotDefinitionError):
        runner.revive(name)
    with pytest.raises(NanobotDefinitionError):
        runner.kill(name)
    assert runner.ledger.records() == [{"keep": True}]


def test_cli_rejects_traversal_with_exit_2(tmp_path, capsys):
    home = tmp_path / "home"
    (home).mkdir()
    victim = tmp_path / "outside.txt"
    assert main(["--home", str(home), "kill", "../outside.txt"]) == 2
    assert main(["--home", str(home), "revive", "../../ledger.jsonl"]) == 2
    assert main(["--home", str(home), "disable", "../x"]) == 2
    assert not victim.exists()
    assert "invalid name" in capsys.readouterr().err


# --- 5. ledger rotation never loses an append ---------------------------------------
def test_append_during_rotation_is_kept(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    for i in range(100):
        led.append({"i": i})
    orig, calls = ledger_mod._sha256, [0]

    def slow(p, opener=open):
        h = orig(p, opener)
        calls[0] += 1
        if calls[0] == 2:  # copy verified, rotating file not yet removed
            Ledger(path).append({"i": "LATE"})
        return h

    monkeypatch.setattr(ledger_mod, "_sha256", slow)
    dest = led.rotate_to_archive(compress=False)
    archived = [json.loads(x) for x in dest.read_text().splitlines()]
    assert len(archived) == 100
    assert Ledger(path).records() == [{"i": "LATE"}]
    assert not list(tmp_path.glob("*.rotating-*"))
    assert not list((tmp_path / "archive").glob("*.partial"))


def test_concurrent_writer_during_rotation_loses_nothing(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    for i in range(5000):
        led.append({"i": i, "pad": "x" * 100})
    written = [5000]
    stop = [False]

    def writer():
        while not stop[0]:
            try:
                Ledger(path).append({"i": written[0]})
                written[0] += 1
            except PermissionError:
                pass

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.01)
    led.rotate_to_archive(compress=False)
    stop[0] = True
    t.join()
    archived = sum(len(f.read_text().splitlines()) for f in (tmp_path / "archive").glob("*.jsonl"))
    live = len(path.read_text().splitlines()) if path.exists() else 0
    assert archived + live == written[0]


def test_failed_verification_leaves_no_partial_and_keeps_records(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    for i in range(10):
        led.append({"i": i})
    orig, calls = ledger_mod._sha256, [0]

    def corrupt(p, opener=open):
        calls[0] += 1
        return "bad" if calls[0] == 2 else orig(p, opener)

    monkeypatch.setattr(ledger_mod, "_sha256", corrupt)
    with pytest.raises(IOError):
        led.rotate_to_archive(compress=True)
    assert not list((tmp_path / "archive").glob("*"))  # no unverified partial
    leftovers = list(tmp_path.glob("ledger.jsonl.rotating-*"))
    assert len(leftovers) == 1 and len(leftovers[0].read_text().splitlines()) == 10
    monkeypatch.setattr(ledger_mod, "_sha256", orig)
    dest = led.rotate_to_archive(compress=False)  # the next call finishes the job
    assert len(dest.read_text().splitlines()) == 10
    assert not list(tmp_path.glob("ledger.jsonl.rotating-*"))


# --- should-fix: COMMS rate limit under concurrency ----------------------------------
def test_comms_rate_limit_holds_across_concurrent_runners(tmp_path):
    home = tmp_path / "home"
    sent, lock = [], threading.Lock()

    def send(action):
        time.sleep(0.05)
        with lock:
            sent.append(action.key)

    bot = make_bot(
        lambda ctx: Result(produced=3, artifact={"n": 1},
                           actions=[Action("msg", key=f"k{i}-{ctx.run_id}", perform=send) for i in range(3)]),
        name="notify", avenue="COMMS", canary=lambda: Canary(input={}, expect_produced=3), rate_limit_per_hour=3)
    with Runner(home) as r:
        r.promote(bot)
    runners = [Runner(home) for _ in range(3)]
    threads = [threading.Thread(target=lambda rr=rr: rr.run_once(bot)) for rr in runners]
    [t.start() for t in threads]
    [t.join() for t in threads]
    [rr.close() for rr in runners]
    assert len(sent) == 3


def test_failed_comms_send_does_not_block_a_retry(runner):
    attempts = []

    def flaky(action):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("smtp down")

    bot = enable(runner, make_bot(
        lambda ctx: Result(produced=1, artifact={"n": 1}, actions=[Action("msg", key="same", perform=flaky)]),
        avenue="COMMS", canary=good_canary))
    assert runner.run_once(bot).verdict == "action_failed"
    assert runner.run_once(bot).actions[0].status == "performed"


# --- should-fix: avenue change resets to CANDIDATE -----------------------------------
def test_avenue_change_requires_repromotion(runner):
    paid = []
    safe = make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), name="payer", avenue="MONEY",
                    canary=good_canary)
    runner.promote(safe)
    swapped = make_bot(lambda ctx: Result(produced=1, actions=[Action("payment", key="z", perform=paid.append)]),
                       name="payer", avenue="GENERAL")
    rep = runner.run_once(swapped)
    assert rep.verdict == "not_enabled" and paid == []
    assert runner.store.get_lifecycle("payer") is Lifecycle.CANDIDATE
    assert "avenue changed" in runner.store.transitions("payer")[-1]["reason"]


# --- should-fix: SystemExit / KeyboardInterrupt in work() ----------------------------
def test_sys_exit_in_work_is_a_recorded_failure(runner):
    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        raise SystemExit(0)

    bot = enable(runner, make_bot(work, canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.verdict == "error" and "SystemExit" in rep.error and rep.exit_code == 1
    assert runner.failure_sink.read()[-1]["kind"] == "exception"


def test_keyboard_interrupt_is_recorded_then_reraised(runner):
    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        raise KeyboardInterrupt

    bot = enable(runner, make_bot(work, canary=good_canary))
    with pytest.raises(KeyboardInterrupt):
        runner.run_once(bot)
    assert runner.ledger.records()[-1]["verdict"] == "error"


def test_cli_exits_nonzero_when_bot_calls_sys_exit(tmp_path, capsys):
    bot_file = tmp_path / "exitbot.py"
    bot_file.write_text(
        "import sys\n"
        "from ultimate_nanobot import Nanobot, Result, Canary\n"
        "class B(Nanobot):\n"
        "    name = 'exiter'\n"
        "    cadence = 60\n"
        "    def work(self, ctx):\n"
        "        if ctx.canary:\n"
        "            return Result(produced=1)\n"
        "        sys.exit(0)\n"
        "    def canary(self):\n"
        "        return Canary(expect_produced=1)\n"
    )
    home = str(tmp_path / "home")
    spec = f"{bot_file}:B"
    assert main(["--home", home, "promote", spec]) == 0
    assert main(["--home", home, "run", spec]) == 1
    assert "SystemExit" in capsys.readouterr().out


# --- should-fix: empty streak cannot be faked ---------------------------------------
def test_bot_cannot_reset_its_own_empty_streak(runner):
    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        ctx.state.set("__empty_streak__", -100)
        return Result(produced=0)

    bot = enable(runner, make_bot(work, canary=good_canary, expected_empty_ok=1))
    verdicts = [runner.run_once(bot).verdict for _ in range(3)]
    assert verdicts == ["ok_empty", "zero_output", "zero_output"]


def test_int_empty_allowance_cannot_be_verified_without_the_store(tmp_path):
    runner = Runner(tmp_path / "h", store=BrokenStore())
    bot = make_bot(lambda ctx: Result(produced=0), avenue="SYSTEM", expected_empty_ok=2)
    assert {runner.run_once(bot).verdict for _ in range(3)} == {"zero_output"}


def test_streak_ignores_non_work_verdicts(runner):
    state = {"fail": False}

    def work(ctx):
        if ctx.canary:
            return Result(produced=1, artifact={"n": 1})
        if state["fail"]:
            raise RuntimeError("x")
        return Result(produced=0)

    bot = enable(runner, make_bot(work, canary=good_canary, expected_empty_ok=2))
    assert runner.run_once(bot).verdict == "ok_empty"
    state["fail"] = True
    runner.run_once(bot)
    state["fail"] = False
    assert runner.run_once(bot).verdict == "ok_empty"
    assert runner.run_once(bot).verdict == "zero_output"


# --- should-fix: bad --input --------------------------------------------------------
@pytest.mark.parametrize("payload", ["{not json", "[1, 2]", "42"])
def test_cli_bad_input_exits_2(tmp_path, capsys, monkeypatch, payload):
    monkeypatch.chdir(ROOT)
    home = str(tmp_path / "home")
    assert main(["--home", home, "run", "examples.page_watcher:bot", "--input", payload]) == 2
    assert main(["--home", home, "loop", "examples.page_watcher:bot", "--input", payload, "--max-ticks", "1"]) == 2
    assert "--input" in capsys.readouterr().err
    assert not (Path(home) / "ledger.jsonl").exists()
