# Roadmap

ultimate-nanobot is meant to grow. The core will stay small and dependency-free;
bigger pieces land as optional extras so a single bot on a Raspberry Pi never
pays for a dashboard it does not use.

## 0.2 - Team and manager digests
- **Teams:** group bots under a team name; one `Result` artifact per bot per window.
- **Manager digest:** a manager step (rules first, optional LLM) reads a team's artifacts
  from the ledger, validates them, applies a veto rule for MONEY teams, and emits ONE JSON
  digest. The digest records which sources contributed, so a dead source cannot hide behind
  one healthy heartbeat.
- **Digest compiler:** merge the window's team digests into a single fleet digest for a human
  (or an issue tracker / chat channel via the existing sinks).

## 0.3 - A Postgres store
- `ultimate_nanobot.stores.postgres.PostgresStore` (optional extra: `pip install ultimate-nanobot[postgres]`).
- Shared fleets: several hosts run bots against one store; the store becomes the message bus.
- Host-aware heartbeats (`name`, `host`) with "freshest beat across hosts" liveness.
- Migrations shipped as plain SQL files.

## 0.4 - Web dashboard
- Read-only fleet page: lifecycle, liveness, last verdict, failure feed, ledger browser.
- Approval inbox: approve / deny pending MONEY and SECURITY actions with an audit trail.
- Kill switch buttons (global and per bot).
- Zero-dependency default (stdlib `http.server`), optional richer frontend.

## 0.5 - MCP server
- An MCP (Model Context Protocol) server so an LLM agent can operate the fleet:
  `status`, `run`, `canary`, `promote`, `disable`, `pending`, and **request** approvals.
- The agent can never approve its own MONEY/SECURITY actions: approval stays a human tool.
- Every agent operation lands in the ledger like any other run.

## Later
- Process-isolated runner: each `work()` in its own subprocess (optionally its own OS
  user), so a hung bot can be killed at `max_seconds` and malicious code cannot reach
  the store or perform side-effects outside its returned Actions.
- Shadow-mode promotion on measured divergence (replay real past inputs, promote when the
  candidate's output diverges less than a threshold from the current bot).
- Docker / process warden example (SYSTEM avenue).
- Inbox triage and digest examples (COMMS avenue).
- Ledger-to-dataset export (instruction/response pairs for fine-tuning your own models).
- Async runner for I/O-heavy fleets.
