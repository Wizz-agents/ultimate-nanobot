# ultimate-nanobot

**Tiny, dependency-free workers that can automate any recurring job, with guardrails.**

A *nanobot* is a small, named, single-purpose worker: it reads some state, does
**one concrete job** on a fixed cadence, and records what it did. ultimate-nanobot
is the framework around it: the part that decides whether the bot is allowed to
run, whether its output counts, whether its side-effects happen, and what gets
written down. Python 3.11+, standard library only.

```python
from ultimate_nanobot import nanobot, Result, Canary

@nanobot(name="greeter", cadence=60, avenue="GENERAL")
def greeter(ctx):
    return Result(produced=1, artifact={"hello": ctx.input.get("who", "world")})

@greeter.canary_probe
def _():
    return Canary(input={"who": "canary"}, expect_artifact={"hello": "canary"})
```

## The manifesto: 8 rules, and why each exists

1. **Produced, not ran.** A run that produced nothing is a failure unless the bot
   declares empty runs as expected. *Why:* "the code ran without errors" is how a
   broken worker stays green for weeks while doing nothing.
2. **Heartbeat only after a completed work unit.** Never at the start, never on the
   error path. Liveness is cadence-relative: OK / LATE (>1.5x cadence) / DARK (>3x) /
   NEVER. *Why:* a heartbeat at the top of the loop reports health the bot has not
   earned; a heartbeat on failure is worse than silence.
3. **Every failure has a path.** The runner catches every exception and escalates it
   to a sink (JSONL file, webhook or any callable); if the sink fails too, a fallback
   file and stderr get it. A bot without a failure path cannot be registered. A failed ledger
   write is escalated too (`ledger_write_failed`, `RunReport.bookkeeping_errors`) but never raised
   out of a run whose actions already happened, and the verdict stays the work's.
   *Why:* swallowed exceptions are the root of most "it was dead and nobody knew".
4. **Earn the right to run.** Bots move DISCOVERED -> CANDIDATE -> ENABLED / DISABLED,
   and only ENABLED bots act. Promotion needs a known-answer `canary()` that the
   **framework** grades; a missing or skipped canary is a refusal. *Why:* a producer
   should never stamp its own success flag. (The canary catches honest bugs; a bot
   written to detect `ctx.canary` can still fake it - see the threat model.)
5. **Money and security fail closed, and their actions need an approval.** If the store
   is unreachable, MONEY and SECURITY bots refuse to run. Every `Action` they return
   waits for an approval, and the default answer is no: a human runs `approve <id>`, or
   the operator configures an approval policy on the `Runner` (a bot can never carry its
   own approver). A run killed mid-action leaves its approval EXECUTING; it is never retried
   automatically (it may have gone through), `pending` lists it as stuck and `release <id>`
   hands it back to a human. One approval performs the action at most once, even across concurrent
   runners; if performing it fails, it needs a fresh approval. *Why:* the cost of a wrong
   payment or an unscoped scan is far larger than the cost of waiting.
6. **Budgets on everything.** Every run has caps on actions, LLM calls and seconds.
   Action and LLM caps are enforced as they happen. The time cap is not a hard
   interrupt: `work()` is not killed mid-flight (call `ctx.check_deadline()` in long
   loops), but a run that overran is discarded after `work()` returns and its actions
   are never performed. *Why:* a bot that suddenly wants 1000 actions instead of 10 is
   malfunctioning, not productive.
7. **One switch stops everything.** A global and a per-bot kill switch (a file or a
   store flag) is checked before every run, and the file switch works even when the
   store is down. *Why:* when something goes wrong you need a brake, not a debugger.
8. **Nothing is deleted, only archived.** Every run (inputs summary, produced count,
   artifact, actions, verdict, duration) is appended to a JSONL ledger. Rotation first
   renames the live file (new runs start a fresh one), then copies, verifies and only
   then removes the renamed file; a failed rotation keeps the records and the next call
   finishes it. *Why:* the ledger is your audit trail and, later, your training
   and evaluation dataset.

## Quickstart

```bash
pip install -e .            # no dependencies; needed for `python -m ultimate_nanobot`
                            # outside the checkout (the examples run from it as-is)
python examples/folder_sorter.py
python examples/page_watcher.py
python examples/approval_payout.py
```

Operate bots from the command line (state lives in `./.nanobot` or `$NANOBOT_HOME`):

```bash
python -m ultimate_nanobot promote examples.approval_payout:bot   # canary-gated
python -m ultimate_nanobot run     examples.approval_payout:bot   # -> pending approval: <id>
python -m ultimate_nanobot pending
python -m ultimate_nanobot approve <id>
python -m ultimate_nanobot release <id>                            # a claim stuck mid-action (see `pending`): back to a human
python -m ultimate_nanobot run     examples.approval_payout:bot   # -> performed (exactly once)
python -m ultimate_nanobot status                                  # fleet + liveness
python -m ultimate_nanobot kill            # global kill switch (or: kill <bot>)
python -m ultimate_nanobot revive
python -m ultimate_nanobot loop examples.page_watcher:bot --input '{"target": "https://example.com"}'  # runs on cadence
python -m ultimate_nanobot archive         # rotate the ledger into ./.nanobot/archive
```

`run` is cron-friendly: exit code 0 = ok, 1 = failed, 3 = skipped (killed / not enabled).
A bot spec is `module:attr` or `path/to/file.py:attr`.

## Writing a bot

```python
from ultimate_nanobot import Nanobot, Avenue, Action, Result, Canary

class DiskReport(Nanobot):
    name = "disk-report"
    cadence = 3600                    # seconds
    avenue = Avenue.SYSTEM
    expected_empty_ok = False         # or True, or an int: allowed empty streak

    def work(self, ctx):
        usage = check_disks(ctx.input.get("paths", ["/"]))
        actions = [Action("alert", key=p, payload={"path": p, "pct": pct}, perform=send_alert)
                   for p, pct in usage.items() if pct > 90]
        return Result(produced=len(usage), artifact={"usage": usage}, actions=actions)

    def canary(self):
        return Canary(input={"paths": ["/"]}, expect_produced=1)
```

What `ctx` gives you: `ctx.input` (dict), `ctx.state.get/set` (persistent per-bot
key/value), `ctx.llm(prompt)` (optional, budgeted), `ctx.check_deadline()`,
`ctx.dry_run`, `ctx.canary`, `ctx.home`, `ctx.log()`.

By convention, bots do not perform side-effects inside `work()`. They return
`Action`s, each with an optional `perform` callable, and the runner decides: dry-run,
approval, dedup, rate limit, budget, kill switch. Only side-effects expressed as
Actions are gated; anything `work()` does directly is not (see the threat model).
An action's id is deterministic (bot + kind + key), so re-proposing the same payment
maps to the same approval; an approval is consumed by an atomic claim, so it cannot
be performed twice.

Plug in your own pieces:

```python
from ultimate_nanobot import Runner, WebhookSink

runner = Runner(
    ".nanobot",
    failure_sink=WebhookSink("https://hooks.example.com/nanobots"),
    llm=lambda prompt, **kw: my_model.complete(prompt),   # optional
    approver=None,   # MONEY/SECURITY policy; default: deny until a human runs `approve <id>`
)
runner.promote(DiskReport())
runner.run_once(DiskReport())
```

## Avenues

| Avenue | Store unreachable | Actions need `approve()` | Extra gates | Default budget (actions / LLM / s) |
|---|---|---|---|---|
| MONEY | fail-closed (always) | yes, default deny | idempotent action ids | 5 / 3 / 60 |
| SECURITY | fail-closed (always) | yes, default deny | idempotent action ids | 5 / 5 / 120 |
| COMMS | fail-closed (can opt to open; never sends ungated) | no | dedup window + hourly rate limit | 10 / 5 / 60 |
| SYSTEM | fail-open | no | - | 20 / 2 / 120 |
| KNOWLEDGE | fail-open | no | - | 50 / 20 / 300 |
| GENERAL | fail-open | no | - | 100 / 10 / 300 |

Fail-open means a store blip cannot dark your maintenance fleet: the bot runs in
degraded mode (kill file still honoured, ledger still written, no heartbeat), and it
performs actions only if the last lifecycle the store confirmed was ENABLED.
Fail-closed means the bot refuses and escalates.

## Examples

| Example | Avenue | Shows |
|---|---|---|
| `examples/folder_sorter.py` | GENERAL | dry-run by default, `--live` to act, never overwrites |
| `examples/page_watcher.py` | KNOWLEDGE | persistent state, change detection, unified-diff artifact, empty runs allowed |
| `examples/approval_payout.py` | MONEY (simulated) | fail-closed, pending approval, exactly-once execution |

## Threat model

ultimate-nanobot guards against **buggy** bots, not **malicious** code.

- `work()` runs in the runner's own process with the runner's privileges. It can read
  files, open sockets or import anything the runner can.
- Only side-effects returned as `Action`s are gated (approval, dedup, rate limit,
  budgets, kill switch, dry-run). A side-effect performed directly inside `work()`
  bypasses all of them.
- A bot can fake its canary (`if ctx.canary: return expected`); the canary proves the
  happy path works, not that the author is honest.
- A bot can reach the store through Python internals and change its own (or another
  bot's) lifecycle or approvals.
- The time budget is checked after `work()` returns; it does not interrupt a hung bot.

If you run code you did not write, isolate it: a separate process or container per
bot, a separate OS user without access to the store, and an approval service the bot
cannot reach. The roadmap tracks a process-isolated runner.

When the store is down, fail-open bots keep running only if the last lifecycle the
store confirmed (cached in `lifecycle_cache.json`) was ENABLED for the same avenue; a
bot with no cached state may work but its actions are not performed. An action that was
being performed when its process crashed stays `EXECUTING` and is never retried
automatically (at most once, never twice).

## Storage

`SQLiteStore` is the default (one file, `./.nanobot/nanobot.db`). Implement the
`Store` interface to use anything else: raise `StoreUnavailable` when the backend is
down and the avenue postures take care of the rest.

## Roadmap

Team and manager digests, a Postgres store, a web dashboard and an MCP server so an
LLM agent can operate the fleet. See [ROADMAP.md](ROADMAP.md).

## Development

```bash
pip install -e ".[test]"
python -m pytest -q
```

## License

MIT, Copyright (c) 2026 ADZ.
