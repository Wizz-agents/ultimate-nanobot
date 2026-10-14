import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import enable, good_canary, make_bot
from ultimate_nanobot import CallableSink, Result, WebhookSink


def _boom(ctx):
    if ctx.canary:
        return Result(produced=1, artifact={"n": 1})
    raise ValueError("disk on fire")


def test_exception_becomes_failure_record_in_jsonl(runner):
    bot = enable(runner, make_bot(_boom, canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.verdict == "error"
    rec = runner.failure_sink.read()[-1]
    assert rec["kind"] == "exception"
    assert rec["bot"] == "test-bot"
    assert rec["run_id"] == rep.run_id
    assert "disk on fire" in rec["message"]
    assert "Traceback" in rec["traceback"]


def test_callable_sink_receives_escalation(runner):
    got = []
    bot = enable(runner, make_bot(_boom, canary=good_canary, failure_sink=CallableSink(got.append)))
    runner.run_once(bot)
    assert got and got[-1]["kind"] == "exception"


def test_webhook_sink_posts_json(runner):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        sink = WebhookSink(f"http://127.0.0.1:{server.server_port}/hook")
        bot = enable(runner, make_bot(_boom, canary=good_canary, failure_sink=sink))
        runner.run_once(bot)
    finally:
        server.shutdown()
    assert received and received[-1]["message"].startswith("ValueError")


def test_broken_sink_falls_back_instead_of_swallowing(runner, capsys):
    def explode(record):
        raise ConnectionError("pager offline")

    bot = enable(runner, make_bot(_boom, canary=good_canary, failure_sink=CallableSink(explode)))
    rep = runner.run_once(bot)
    assert rep.verdict == "error"
    fallback = runner.fallback_sink.read()
    assert fallback[-1]["kind"] == "exception"
    assert "pager offline" in fallback[-1]["sink_error"]
    assert "failure sink error" in capsys.readouterr().err


def test_failing_action_is_escalated_and_not_green(runner):
    from ultimate_nanobot import Action

    def bad(action):
        raise PermissionError("nope")

    bot = enable(runner, make_bot(
        lambda ctx: Result(produced=1, artifact={"n": 1}, actions=[Action("write", perform=bad)]),
        canary=good_canary))
    rep = runner.run_once(bot)
    assert rep.verdict == "action_failed"
    assert not rep.heartbeat
    assert runner.failure_sink.read()[-1]["kind"] == "action_failed"
