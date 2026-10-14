import gzip
import json
from pathlib import Path

import pytest

from conftest import ROOT, enable, good_canary, make_bot
from ultimate_nanobot import Ledger, Result, Runner, rotate_to_archive
from ultimate_nanobot.cli import main

REQUIRED = {"ts", "run_id", "bot", "avenue", "mode", "input_summary", "produced", "artifact",
            "actions", "verdict", "duration_s", "llm_calls"}


# --- ledger --------------------------------------------------------------
def test_every_run_path_writes_a_full_ledger_record(runner):
    bot = make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), canary=good_canary)
    runner.run_once(bot, input={"q": 1})  # not_enabled
    runner.promote(bot)                   # canary run
    runner.run_once(bot, input={"q": 2})  # ok
    runner.kill()
    runner.run_once(bot)                  # killed
    recs = runner.ledger.records()
    assert [r["verdict"] for r in recs] == ["not_enabled", "ok", "ok", "killed"]
    assert [r["mode"] for r in recs][1] == "canary"
    for r in recs:
        assert REQUIRED <= set(r)
    assert recs[2]["input_summary"] == {"q": 2}


def test_ledger_is_append_only(runner):
    bot = enable(runner, make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), canary=good_canary))
    runner.run_once(bot)
    before = runner.ledger.path.read_bytes()
    runner.run_once(bot)
    after = runner.ledger.path.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    assert not any(hasattr(Ledger, m) for m in ("delete", "remove", "truncate", "update"))


@pytest.mark.parametrize("compress", [True, False])
def test_rotate_to_archive_keeps_every_record(tmp_path, compress):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    for i in range(5):
        ledger.append({"i": i})
    original = ledger.path.read_bytes()
    dest = rotate_to_archive(ledger.path, tmp_path / "archive", compress=compress)
    data = gzip.decompress(dest.read_bytes()) if compress else dest.read_bytes()
    assert data == original
    assert not ledger.path.exists()
    ledger.append({"i": 5})
    assert ledger.records() == [{"i": 5}]
    assert rotate_to_archive(tmp_path / "missing.jsonl", tmp_path / "archive") is None


# --- CLI -----------------------------------------------------------------
def test_cli_full_approval_flow(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    home = str(tmp_path / "home")
    spec = "examples.approval_payout:bot"
    assert main(["--home", home, "run", spec]) == 3  # not enabled yet
    assert main(["--home", home, "promote", spec]) == 0
    assert main(["--home", home, "run", spec]) == 0
    out = capsys.readouterr().out
    action_id = out.split("pending approval: ")[1].split()[0]
    assert main(["--home", home, "approve", action_id]) == 0
    assert main(["--home", home, "run", spec]) == 0
    assert "payment:performed" in capsys.readouterr().out
    assert main(["--home", home, "status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["liveness"] == "OK" and rows[0]["lifecycle"] == "ENABLED"
    assert main(["--home", home, "disable", "approval-payout"]) == 0
    assert main(["--home", home, "run", spec]) == 3


def test_cli_canary_kill_and_archive(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    home = str(tmp_path / "home")
    spec = str(ROOT / "examples" / "page_watcher.py") + ":bot"  # file-path spec form
    assert main(["--home", home, "canary", spec]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["--home", home, "kill"]) == 0
    assert main(["--home", home, "promote", spec]) == 0
    page = tmp_path / "p.txt"
    page.write_text("x")
    assert main(["--home", home, "run", spec, "--input", json.dumps({"target": str(page)})]) == 3
    assert main(["--home", home, "revive"]) == 0
    assert main(["--home", home, "run", spec, "--input", json.dumps({"target": str(page)})]) == 0
    assert main(["--home", home, "archive"]) == 0
    assert list((Path(home) / "archive").glob("ledger-*.jsonl.gz"))


def test_loop_runs_on_cadence(tmp_path):
    clock = {"t": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    with Runner(tmp_path / "h", clock=lambda: clock["t"]) as runner:
        fast = enable(runner, make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), name="fast",
                                       cadence=10, canary=good_canary))
        slow = enable(runner, make_bot(lambda ctx: Result(produced=1, artifact={"n": 1}), name="slow",
                                       cadence=30, canary=good_canary))
        runner.loop([fast, slow], max_ticks=4, sleep=sleep)
        bots = [r["bot"] for r in runner.ledger.records() if r["mode"] == "live"]
    assert bots.count("fast") == 4 and bots.count("slow") == 2


# --- examples ------------------------------------------------------------
def test_folder_sorter_dry_run_then_live(tmp_path):
    from examples.folder_sorter import bot

    root = tmp_path / "inbox"
    root.mkdir()
    for name in ("a.jpg", "b.pdf", "c.weird"):
        (root / name).write_text("x")
    (root / "images").mkdir()
    (root / "images" / "a.jpg").write_text("already here")  # must not be overwritten
    with Runner(tmp_path / "h") as runner:
        assert runner.promote(bot).passed
        dry = runner.run_once(bot, input={"root": str(root)})
        assert dry.produced == 2 and (root / "a.jpg").exists()
        live = runner.run_once(bot, input={"root": str(root)}, live=True)
    assert live.ok
    assert (root / "images" / "a.jpg").read_text() == "already here"
    assert (root / "images" / "a (1).jpg").exists()
    assert (root / "documents" / "b.pdf").exists()
    assert (root / "c.weird").exists()


def test_page_watcher_emits_diff(tmp_path):
    from examples.page_watcher import bot

    page = tmp_path / "page.txt"
    page.write_text("line one\nline two\n")
    with Runner(tmp_path / "h") as runner:
        runner.promote(bot)
        assert runner.run_once(bot, input={"target": str(page)}).artifact["baseline"] is True
        assert runner.run_once(bot, input={"target": str(page)}).verdict == "ok_empty"
        page.write_text("line one\nline 2\n")
        art = runner.run_once(bot, input={"target": str(page)}).artifact
    assert art["changed"] and "+line 2" in art["diff"] and "-line two" in art["diff"]


def test_page_watcher_missing_target_is_a_failure(tmp_path):
    from examples.page_watcher import bot

    with Runner(tmp_path / "h") as runner:
        runner.promote(bot)
        rep = runner.run_once(bot, input={"target": str(tmp_path / "nope.txt")})
    assert rep.verdict == "error"


@pytest.mark.parametrize("module", ["folder_sorter", "page_watcher", "approval_payout"])
def test_example_demos_run(module):
    import importlib

    line = importlib.import_module(f"examples.{module}").demo()
    assert line.startswith(module)
