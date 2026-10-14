"""page_watcher (KNOWLEDGE avenue): detect a change in a local file or an HTTP
page and emit a unified-diff artifact.

Plain GET only: it respects whatever the page serves and never tries to get
around logins, captchas or rate limits.

    python -m ultimate_nanobot promote examples.page_watcher:bot
    python -m ultimate_nanobot run examples.page_watcher:bot --input '{"target": "https://example.com"}'

Or run this file for a self-contained demo:

    python examples/page_watcher.py
"""

from __future__ import annotations

import difflib
import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ultimate_nanobot import Avenue, Canary, Nanobot, Result, Runner  # noqa: E402

MAX_BYTES = 2 * 1024 * 1024
MAX_DIFF_LINES = 400


def fetch(target: str, timeout: float = 15.0) -> str:
    if target.startswith(("http://", "https://")):
        req = urllib.request.Request(target, headers={"User-Agent": "ultimate-nanobot/0.1 page-watcher"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-chosen target
            return resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    return Path(os.path.expanduser(target)).read_text(encoding="utf-8", errors="replace")[:MAX_BYTES]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PageWatcher(Nanobot):
    name = "page-watcher"
    cadence = 60 * 60
    avenue = Avenue.KNOWLEDGE
    expected_empty_ok = True  # "no change" is the normal answer; a failed fetch still raises
    description = "Emit a diff artifact when a watched page changes."

    def work(self, ctx) -> Result:
        target = ctx.input.get("target") or os.environ.get("PAGE_WATCHER_TARGET")
        if not target:
            raise ValueError("set input.target or PAGE_WATCHER_TARGET")
        text = fetch(target)
        key = "snapshot:" + _sha(target)[:12]
        prev = ctx.state.get(key)
        if prev is None and "baseline_text" in ctx.input:
            prev = {"sha": _sha(ctx.input["baseline_text"]), "text": ctx.input["baseline_text"]}
        new_sha = _sha(text)
        if prev is None:
            ctx.state.set(key, {"sha": new_sha, "text": text})
            return Result(produced=1, artifact={"target": target, "changed": False, "baseline": True, "sha": new_sha})
        if prev["sha"] == new_sha:
            return Result(produced=0, artifact={"target": target, "changed": False, "sha": new_sha})
        diff = list(
            difflib.unified_diff(
                prev["text"].splitlines(), text.splitlines(), "before", "after", lineterm=""
            )
        )
        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
        ctx.state.set(key, {"sha": new_sha, "text": text})
        return Result(
            produced=1,
            artifact={
                "target": target,
                "changed": True,
                "old_sha": prev["sha"],
                "new_sha": new_sha,
                "added": added,
                "removed": removed,
                "diff": diff[:MAX_DIFF_LINES],
                "diff_truncated": len(diff) > MAX_DIFF_LINES,
            },
        )

    def canary(self) -> Canary:
        fd, path = tempfile.mkstemp(prefix="nanobot-canary-page-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("alpha\nbeta\n")
        return Canary(
            input={"target": path, "baseline_text": "alpha\ngamma\n"},
            expect_produced=1,
            expect_artifact={"changed": True, "added": 1, "removed": 1},
        )


bot = PageWatcher()


def demo() -> str:
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as pages:
        page = Path(pages, "page.html")
        page.write_text("<h1>Prices</h1>\n<p>Widget: 10</p>\n", encoding="utf-8")
        runner = Runner(home)
        runner.promote(bot)
        first = runner.run_once(bot, input={"target": str(page)})
        second = runner.run_once(bot, input={"target": str(page)})
        page.write_text("<h1>Prices</h1>\n<p>Widget: 12</p>\n", encoding="utf-8")
        third = runner.run_once(bot, input={"target": str(page)})
        a = third.artifact
        changed_line = next(d for d in a["diff"] if d.startswith("+") and not d.startswith("+++"))
        runner.close()
        return (
            f"page_watcher: baseline={first.verdict} -> unchanged={second.verdict} -> "
            f"changed={a['changed']} (+{a['added']}/-{a['removed']}, {changed_line!r})"
        )


if __name__ == "__main__":
    print(demo())
