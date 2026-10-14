# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-14

### Hardened before release (adversarial review)
- Approvals are claimed atomically (`APPROVED -> EXECUTING -> EXECUTED | FAILED`): one
  approval performs an action at most once across concurrent runners; a failed perform
  needs a fresh approval instead of retrying every run.
- Bots can no longer carry their own `approver`; approval policy belongs to the `Runner`.
- Store outage: fail-open bots act only if a local lifecycle cache confirms ENABLED for
  the same avenue; unknown bots work but do not act.
- Bot names are validated wherever they become file names (kill switch, cache).
- Ledger rotation renames the live file first and never leaves an unverified archive.
- COMMS dedup + rate limit are checked and reserved in one `BEGIN IMMEDIATE` transaction.
- Changing a bot's avenue drops it back to CANDIDATE.
- `SystemExit` / `KeyboardInterrupt` inside `work()` are recorded as failures.
- The empty-run streak is derived from the runs table, not bot-writable state.
- Invalid `--input` JSON or bot names exit with code 2 and a clean error.
- README: threat model; claims reworded to match the code.

First release.

### Added
- `Nanobot` base class and `@nanobot` decorator (name, cadence, avenue, `work(ctx)` returning `Result`).
- Produced-not-ran rule: zero output is a failure unless `expected_empty_ok` (bool or allowed streak).
- Heartbeats only after a completed work unit; cadence-relative liveness verdicts OK / LATE / DARK / NEVER.
- Built-in failure path: every exception becomes a failure record escalated to a sink
  (`JsonlSink` default, `WebhookSink`, `CallableSink`), with a fallback file if the sink itself fails.
  A bot without a failure path cannot be registered.
- Lifecycle DISCOVERED -> CANDIDATE -> ENABLED / DISABLED with framework-graded canary promotion;
  a missing, skipped or expectation-free canary is a refusal.
- Avenue postures: MONEY and SECURITY are fail-closed with a required approval for every action;
  COMMS dedups and rate-limits; every avenue has per-run budget caps (actions, LLM calls, seconds).
- Global and per-bot kill switch (file flag or store flag), checked before every run.
- Append-only JSONL run ledger and a verified archive-then-trim rotation helper.
- Optional `ctx.llm()` hook routed to any user-supplied callable, counted against the budget.
- `Store` interface with an SQLite default.
- CLI: `run`, `loop`, `status`, `canary`, `promote`, `disable`, `approve`, `pending`, `kill`,
  `revive`, `archive`.
- Examples: `folder_sorter` (GENERAL), `page_watcher` (KNOWLEDGE), `approval_payout` (MONEY, simulated).
