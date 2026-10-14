"""Failure sinks: where escalations go. JSONL file (default), webhook, or any callable."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Callable


class FailureSink:
    """Anything with emit(record: dict). emit may raise; the runner then falls back."""

    def emit(self, record: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class JsonlSink(FailureSink):
    """Append one JSON object per line. The default sink."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def emit(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, sort_keys=True)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class WebhookSink(FailureSink):
    """POST the record as JSON (Slack/Discord/ntfy-style receivers, your own API...)."""

    def __init__(self, url: str, timeout: float = 5.0, headers: dict | None = None) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("webhook url must be http(s)")
        self.url = url
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def emit(self, record: dict) -> None:
        body = json.dumps(record, default=str).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - user-supplied URL
            if resp.status >= 300:
                raise RuntimeError(f"webhook returned HTTP {resp.status}")


class CallableSink(FailureSink):
    """Wrap any function, e.g. your pager, chat bot or issue tracker client."""

    def __init__(self, fn: Callable[[dict], None]) -> None:
        if not callable(fn):
            raise TypeError("CallableSink needs a callable")
        self.fn = fn

    def emit(self, record: dict) -> None:
        self.fn(record)
