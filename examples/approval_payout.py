"""approval_payout (MONEY avenue, SIMULATED): proposes a fake payment that stays
blocked until a human approves it. Demonstrates fail-closed + approve().

No real money moves: "performing" the action appends a line to
simulated_payouts.jsonl. Swap _simulate_payment for a real client only when
you mean it - the approval gate stays in front of it either way.

    python -m ultimate_nanobot promote examples.approval_payout:bot
    python -m ultimate_nanobot run examples.approval_payout:bot        # -> pending approval: <id>
    python -m ultimate_nanobot approve <id>
    python -m ultimate_nanobot run examples.approval_payout:bot        # -> performed
    python -m ultimate_nanobot run examples.approval_payout:bot        # -> already_executed

Or run this file for a self-contained demo:

    python examples/approval_payout.py
"""

from __future__ import annotations

import functools
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ultimate_nanobot import Action, Avenue, Canary, Nanobot, Result, Runner  # noqa: E402

# XTS is the ISO 4217 code reserved for testing: it can never be a real payment.
DEFAULT_INVOICES = [
    {"invoice": "INV-0001", "payee": "Example Vendor (fictional)", "amount_minor": 1250, "currency": "XTS"},
]


def _simulate_payment(log_path: str, action: Action) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "action_id": action.id, **action.payload}) + "\n")


class ApprovalPayout(Nanobot):
    name = "approval-payout"
    cadence = 24 * 60 * 60
    avenue = Avenue.MONEY  # fail-closed + every action needs approval
    description = "Propose (simulated) invoice payments; nothing pays without a human approval."

    def work(self, ctx) -> Result:
        invoices = ctx.input.get("invoices") or DEFAULT_INVOICES
        log_path = ctx.input.get("payout_log") or str(Path(ctx.home) / "simulated_payouts.jsonl")
        actions = [
            Action(
                kind="payment",
                key=inv["invoice"],  # same invoice -> same action id -> can never pay twice
                payload=inv,
                perform=functools.partial(_simulate_payment, log_path),
                description=f"SIMULATED pay {inv['amount_minor']} {inv['currency']} to {inv['payee']} ({inv['invoice']})",
            )
            for inv in invoices
        ]
        return Result(
            produced=len(actions),
            artifact={
                "proposed": len(actions),
                "total_minor": sum(i["amount_minor"] for i in invoices),
                "simulated": True,
            },
            actions=actions,
        )

    def canary(self) -> Canary:
        return Canary(
            input={"invoices": [{"invoice": "CANARY-1", "payee": "nobody", "amount_minor": 500, "currency": "XTS"}]},
            expect_produced=1,
            expect_artifact={"proposed": 1, "total_minor": 500, "simulated": True},
            expect_action_kinds=["payment"],
        )


bot = ApprovalPayout()


def demo() -> str:
    with tempfile.TemporaryDirectory() as home:
        runner = Runner(home)
        runner.promote(bot)
        first = runner.run_once(bot)
        pending = first.actions[0]
        assert pending.status == "pending_approval"
        assert not (Path(home) / "simulated_payouts.jsonl").exists()
        runner.approve(pending.id, by="demo")
        second = runner.run_once(bot)
        third = runner.run_once(bot)
        paid = (Path(home) / "simulated_payouts.jsonl").read_text().count("\n")
        runner.close()
        return (
            f"approval_payout: run1={pending.status} (id {pending.id}) -> approved -> "
            f"run2={second.actions[0].status} -> run3={third.actions[0].status}; simulated payments={paid}"
        )


if __name__ == "__main__":
    print(demo())
