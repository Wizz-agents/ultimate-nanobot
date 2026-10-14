"""Command line: python -m ultimate_nanobot <command> ...

Exit codes (cron friendly): 0 ok, 1 failed, 2 usage error, 3 skipped (killed / not enabled).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import __version__
from .ledger import rotate_to_archive
from .liveness import fleet_status
from .core import NanobotDefinitionError
from .runner import Runner, load_bot


class UsageError(Exception):
    """Bad command-line input: printed cleanly, exit code 2."""


def _parse_input(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--input is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise UsageError("--input must be a JSON object, e.g. '{\"root\": \"~/Downloads\"}'")
    return payload


def _runner(args) -> Runner:
    runner = Runner(args.home)
    args._runners.append(runner)
    return runner


def _print_report(rep) -> None:
    acts = ", ".join(f"{a.kind}:{a.status}" for a in rep.actions) or "-"
    line = f"{rep.bot} run={rep.run_id} verdict={rep.verdict} mode={rep.mode} produced={rep.produced} actions=[{acts}]"
    if rep.error:
        line += f" error={rep.error}"
    print(line)
    for a in rep.actions:
        if a.status == "pending_approval":
            print(f"  pending approval: {a.id}  ({a.description or a.kind})")


def cmd_run(args) -> int:
    payload = _parse_input(args.input)
    bot = load_bot(args.bot)
    rep = _runner(args).run_once(bot, input=payload, live=args.live, shadow=args.shadow)
    _print_report(rep)
    return rep.exit_code


def cmd_loop(args) -> int:
    payload = _parse_input(args.input)
    bots = [load_bot(spec) for spec in args.bots]
    runs = _runner(args).loop(
        bots, max_ticks=args.max_ticks, live=args.live, input=payload, on_report=_print_report
    )
    print(f"loop finished after {runs} run(s)")
    return 0


def cmd_status(args) -> int:
    runner = _runner(args)
    for spec in args.bots or []:
        runner.register(load_bot(spec))
    rows = fleet_status(runner.store)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no nanobots registered yet")
        return 0
    cols = ["name", "avenue", "lifecycle", "liveness", "last_heartbeat_age_s", "last_verdict", "killed"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    return 0


def cmd_canary(args) -> int:
    rep = _runner(args).run_canary(load_bot(args.bot))
    print(f"{rep.bot} canary: {'PASS' if rep.passed else 'FAIL'} - {rep.detail}")
    return 0 if rep.passed else 1


def cmd_promote(args) -> int:
    runner = _runner(args)
    bot = load_bot(args.bot)
    rep = runner.promote(bot)
    state = runner.store.get_lifecycle(bot.name).value
    print(f"{bot.name}: {state} ({rep.detail})")
    return 0 if rep.passed else 1


def cmd_disable(args) -> int:
    runner = _runner(args)
    name = args.bot
    if ":" in name:
        bot = load_bot(name)
        runner.register(bot)
        name = bot.name
    runner.disable(name, args.reason)
    print(f"{name}: DISABLED")
    return 0


def cmd_approve(args) -> int:
    runner = _runner(args)
    try:
        row = runner.deny(args.action_id, by=args.by) if args.deny else runner.approve(args.action_id, by=args.by)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{row['action_id']}: {row['status']} ({row['action'].get('description') or row['action'].get('kind')})")
    return 0


def cmd_pending(args) -> int:
    rows = _runner(args).store.pending_approvals()
    if not rows:
        print("no actions awaiting approval")
    for r in rows:
        a = r["action"]
        print(f"{r['action_id']}  {r['bot']}  {a.get('kind')}  {a.get('description') or json.dumps(a.get('payload'))}")
    return 0


def cmd_kill(args) -> int:
    _runner(args).kill(args.bot)
    print(f"kill switch ON for {args.bot or 'ALL bots'}")
    return 0


def cmd_revive(args) -> int:
    _runner(args).revive(args.bot)
    print(f"kill switch OFF for {args.bot or 'ALL bots'}")
    return 0


def cmd_archive(args) -> int:
    runner = _runner(args)
    dest = rotate_to_archive(runner.ledger.path, runner.home / "archive")
    print(f"archived to {dest}" if dest else "ledger empty; nothing to archive")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ultimate_nanobot", description="Run and operate a fleet of nanobots.")
    p.add_argument("--version", action="version", version=f"ultimate-nanobot {__version__}")
    p.add_argument(
        "--home",
        default=os.environ.get("NANOBOT_HOME", ".nanobot"),
        help="state directory (store, ledger, failures, flags); env NANOBOT_HOME",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("run", help="run one work unit (cron friendly)")
    s.add_argument("bot", help="module:attr or path/to/file.py:attr")
    s.add_argument("--input", help="JSON object passed as ctx.input")
    s.add_argument("--live", action="store_true", help="override a bot's dry-run default")
    s.add_argument("--shadow", action="store_true", help="run a not-yet-enabled bot without acting")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("loop", help="run bots on their cadence")
    s.add_argument("bots", nargs="+")
    s.add_argument("--max-ticks", type=int, default=None)
    s.add_argument("--input", help="JSON object passed as ctx.input to every bot")
    s.add_argument("--live", action="store_true")
    s.set_defaults(fn=cmd_loop)

    s = sub.add_parser("status", help="fleet table with liveness verdicts")
    s.add_argument("bots", nargs="*", help="optionally register these specs first")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("canary", help="run and grade a bot's known-answer probe")
    s.add_argument("bot")
    s.set_defaults(fn=cmd_canary)

    s = sub.add_parser("promote", help="canary-gated promotion to ENABLED")
    s.add_argument("bot")
    s.set_defaults(fn=cmd_promote)

    s = sub.add_parser("disable", help="set a bot DISABLED (name or spec)")
    s.add_argument("bot")
    s.add_argument("--reason", default="disabled by operator")
    s.set_defaults(fn=cmd_disable)

    s = sub.add_parser("approve", help="approve (or --deny) a pending action")
    s.add_argument("action_id")
    s.add_argument("--deny", action="store_true")
    s.add_argument("--by", default=os.environ.get("USER") or os.environ.get("USERNAME") or "cli")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("pending", help="list actions awaiting approval")
    s.set_defaults(fn=cmd_pending)

    s = sub.add_parser("kill", help="kill switch on (one bot, or all)")
    s.add_argument("bot", nargs="?")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("revive", help="kill switch off (one bot, or all)")
    s.add_argument("bot", nargs="?")
    s.set_defaults(fn=cmd_revive)

    s = sub.add_parser("archive", help="rotate the ledger into the archive (verified copy)")
    s.set_defaults(fn=cmd_archive)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    args._runners = []
    try:
        return args.fn(args)
    except (UsageError, NanobotDefinitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        for runner in args._runners:
            runner.close()
