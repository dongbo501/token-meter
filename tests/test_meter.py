import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from codex_runner import stream_usage
from ledger import Ledger
from metering import endpoint, usage
from worker import complete, deliver, retry_after


class Gateway(BaseHTTPRequestHandler):
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, data, self.headers.get("Authorization")))
        code, result = self.server.reply
        self.send_response(code)
        if code == 429:
            self.send_header("Retry-After", "60")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, *_):
        pass


class MeterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger"
        self.now = datetime(2026, 11, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.book = self.open()
        self.addCleanup(lambda: self.book.db.close())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        ).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.server.calls = []
        self.server.reply = (
            200,
            {
                "usage": {
                    "prompt_tokens": 90,
                    "completion_tokens": 16,
                    "prompt_tokens_details": {"cached_tokens": 8},
                }
            },
        )
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.book.save(
            {
                "base_url": self.base,
                "api_key": "fixture",
                "model": "mock-chat",
                "api_style": "chat",
                "input_price": 1,
                "output_price": 2,
                "cached_price": 0.25,
                "bark_url": self.base + "/device-key",
                "concurrency": 3,
            }
        )

    def open(self):
        return Ledger(self.path, clock=lambda: self.now.timestamp())

    def start(self, **changes):
        if changes:
            self.book.save(changes)
        self.book.switch(True)

    def test_chat_and_cache_accounting(self):
        self.start(output_field="max_completion_tokens")
        job = self.book.reserve()
        complete(self.book, job)
        path, data, auth = self.server.calls[0]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer fixture"))
        self.assertEqual(data["max_completion_tokens"], 2048)
        self.assertNotEqual(job[2].split(" ")[0], self.book.reserve()[2].split(" ")[0])
        state = self.book.snapshot()
        self.assertEqual(state["totals"]["pico"], 116000000)
        self.assertEqual(state["totals"]["cached"], 8)
        self.assertNotIn("fixture", json.dumps(state))
        self.assertNotIn("device-key", json.dumps(state))

    def test_concurrent_reservations_and_pause(self):
        self.start(
            mode="tokens",
            token_target=3500,
            token_limit=4000,
            concurrency=32,
            max_output=700,
        )
        with ThreadPoolExecutor(max_workers=16) as pool:
            jobs = [
                job for job in pool.map(lambda _: self.book.reserve(), range(32)) if job
            ]
        self.assertGreater(len(jobs), 1)
        self.assertLessEqual(self.book.snapshot()["held_tokens"], 4000)
        self.book.switch(False)
        self.assertIsNone(self.book.reserve())
        with self.assertRaises(ValueError):
            self.book.save({"model": "changed"})
        for ident, *_ in jobs:
            self.book.settle(ident, 20, 0, 1)
        self.assertEqual(self.book.snapshot()["active"], 0)

    def test_token_target_recurrence_and_goal_dedup(self):
        self.start(mode="tokens", token_target=100, token_limit=1200)
        complete(self.book, self.book.reserve())
        self.assertIsNone(self.book.reserve())
        self.assertEqual(self.book.snapshot()["status"], "reached")
        self.book.switch(False)
        self.book.switch(True)
        self.assertEqual(self.book.snapshot()["pending_notices"], 1)
        self.server.reply = 200, {"code": 200}
        deliver(self.book, self.book.next_notice())
        path, data, _ = self.server.calls[-1]
        self.assertEqual(
            (path, data["device_key"], data["title"]),
            ("/push", "device-key", "本周用量目标已完成"),
        )
        self.now += timedelta(days=7)
        self.assertEqual(self.book.snapshot()["spent"], 0)
        job = self.book.reserve()
        self.book.settle(job[0], 96, 0, 10)
        self.assertIsNotNone(self.book.next_notice())

    def test_usd_threshold(self):
        self.start(usd_target=0.01, usd_limit=0.02, input_price=100, output_price=100)
        job = self.book.reserve()
        self.assertIsNotNone(job)
        self.assertLessEqual(self.book.snapshot()["held_usd"], 0.02)
        self.book.settle(job[0], 95, 0, 10)
        self.assertEqual(self.book.snapshot()["status"], "reached")
        self.assertEqual(self.book.snapshot()["spent"], 0.0105)
        self.assertEqual(self.book.next_notice()[0]["title"], "本周用量目标已完成")

    def test_rate_limit_backoff_and_bark_retry_survive_restart(self):
        self.start()
        self.server.reply = 429, {}
        complete(self.book, self.book.reserve())
        self.assertEqual(self.book.snapshot()["status"], "backoff")
        self.assertIsNone(self.book.reserve())
        self.server.reply = 503, {}
        deliver(self.book, self.book.next_notice())
        self.assertEqual(self.book.snapshot()["pending_notices"], 1)
        self.book.db.close()
        self.book = self.open()
        self.assertIsNone(self.book.next_notice())
        self.now += timedelta(seconds=61)
        self.server.reply = 200, {"code": 200}
        deliver(self.book, self.book.next_notice())
        self.assertEqual(self.book.snapshot()["pending_notices"], 0)
        self.assertIsNotNone(self.book.reserve())

    def test_missing_usage_manual_reconciliation(self):
        self.start()
        self.server.reply = 200, {"choices": []}
        job = self.book.reserve()
        complete(self.book, job)
        self.assertEqual(self.book.snapshot()["unknown"], 1)
        self.assertGreater(self.book.snapshot()["held_usd"], 0)
        with self.assertRaises(ValueError):
            self.book.switch(True)
        self.book.settle(job[0], 40, 2, 10, manual=True)
        with self.assertRaises(ValueError):
            self.book.settle(job[0], 40, 2, 10, manual=True)
        self.assertEqual(self.book.snapshot()["held_tokens"], 0)
        self.assertEqual(self.book.snapshot()["totals"]["count"], 1)
        self.book.switch(True)

    def test_interrupted_job_is_held_across_week_boundary(self):
        self.start()
        job = self.book.reserve()
        self.book.db.close()
        self.now += timedelta(days=7)
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["status"], "review")
        self.assertIsNone(self.book.reserve())
        self.assertEqual(self.book.snapshot()["pending_notices"], 1)
        self.book.settle(job[0], 50, 0, 22, manual=True)
        self.assertEqual(self.book.snapshot()["spent"], 0)

    def test_bad_key_releases_reservation_and_notifies(self):
        self.start()
        self.server.reply = 401, {"message": "never log raw responses"}
        complete(self.book, self.book.reserve())
        state = self.book.snapshot()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["held_tokens"], 0)
        self.assertIn("HTTP 401", self.book.next_notice()[0]["body"])
        self.assertNotIn("never log", json.dumps(state))

    def test_tail_and_week_boundary(self):
        self.now = datetime(2026, 11, 22, 23, 59, 59, tzinfo=self.book.zone)
        self.start(mode="tokens", token_target=10, token_limit=10)
        self.assertIsNone(self.book.reserve())
        self.assertEqual(self.book.snapshot()["status"], "tail")
        self.assertIn("余量", self.book.next_notice()[0]["title"])
        self.now += timedelta(seconds=1)
        self.book.reserve()
        self.assertEqual(self.book.snapshot()["week"], "2026-11-23")

    def test_validation(self):
        self.assertEqual(
            endpoint("https://example.com/custom/chat/completions/"),
            "https://example.com/custom/chat/completions",
        )
        self.assertEqual(
            usage({"prompt_tokens": 2, "completion_tokens": 1}), (2, None, 1)
        )
        with self.assertRaises(ValueError):
            usage(
                {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 10},
                }
            )
        with self.assertRaises(ValueError):
            self.book.save({"base_url": "https://example.net/v1"})
        with self.assertRaises(ValueError):
            self.book.save({"concurrency": 2.5})
        self.assertEqual(retry_after(None), 0)

    def test_responses_usage_and_endpoint(self):
        self.start(api_style="responses")
        self.server.reply = (
            200,
            {
                "usage": {
                    "input_tokens": 90,
                    "output_tokens": 16,
                    "input_tokens_details": {"cached_tokens": 8},
                }
            },
        )
        complete(self.book, self.book.reserve())
        path, data, _ = self.server.calls[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(data["max_output_tokens"], 2048)
        self.assertFalse(data["store"])
        self.assertNotIn("messages", data)
        self.assertEqual(self.book.snapshot()["totals"]["pico"], 116000000)
        self.assertEqual(
            endpoint("https://example.com/custom/responses/", "responses"),
            "https://example.com/custom/responses",
        )
        self.assertEqual(
            endpoint("https://example.com/v1/chat/completions", "responses"),
            "https://example.com/v1/responses",
        )

    def test_probe_is_billed_once_without_enabling_schedule(self):
        job = self.book.reserve(probe=True)
        self.assertEqual(job[3], 32)
        with self.assertRaises(ValueError):
            self.book.reserve(probe=True)
        complete(self.book, job)
        state = self.book.snapshot()
        self.assertEqual(state["totals"]["count"], 1)
        self.assertFalse(state["enabled"])

    def test_bark_is_quiet_for_start_progress_pause_and_new_week(self):
        self.start()
        complete(self.book, self.book.reserve())
        self.book.switch(False)
        self.now += timedelta(days=7)
        self.book.switch(True)
        self.assertEqual(self.book.snapshot()["pending_notices"], 0)

    def test_cli_reservation_uses_full_input_and_shared_cap(self):
        self.start(
            api_style="codex", mode="tokens", token_target=9000, token_limit=10000
        )
        first, second = self.book.reserve(), self.book.reserve()
        self.book.resize(first[0], first[1], "x" * 5000)
        self.assertLessEqual(self.book.snapshot()["held_tokens"], 10000)
        with self.assertRaises(ValueError):
            self.book.resize(second[0], second[1], "y" * 12000)
        self.assertLessEqual(self.book.snapshot()["held_tokens"], 10000)

    def test_old_configuration_keeps_chat_after_migration(self):
        cfg = json.loads(self.book._state()["config"])
        cfg.pop("api_style")
        with self.book.db:
            self.book.db.execute("UPDATE state SET config=?", (json.dumps(cfg),))
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["config"]["api_style"], "chat")

    def test_usage_overrun_pauses_and_keeps_actual_cost(self):
        self.start()
        job = self.book.reserve()
        self.book.settle(job[0], 30000, 0, 4000)
        state = self.book.snapshot()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["totals"]["prompt"], 30000)
        self.assertIn("输出上限", state["note"])

    def test_output_limit_violation_is_detected_despite_input_headroom(self):
        self.start(max_output=32)
        job = self.book.reserve()
        self.book.settle(job[0], 30, 0, 146)
        state = self.book.snapshot()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["totals"]["output"], 146)
        self.assertIn("输出上限", state["note"])

    def cli_fixture(self, input_size=4000):
        test = self

        class FakeCLI:
            def __init__(self, args, env, **kwargs):
                self.env = env
                test.assertNotIn("fixture", json.dumps(env))
                value = next(
                    a for a in args if a.startswith("model_providers.meter.base_url=")
                )
                self.url = json.loads(value.split("=", 1)[1]) + "/responses"

            def communicate(self, _prompt, timeout):
                body = {
                    "model": "mock",
                    "instructions": "x" * input_size,
                    "input": "Return Python code",
                    "tools": [{"type": "function"}],
                }
                headers = {
                    "Authorization": "Bearer " + self.env["METER_RELAY_KEY"],
                    "Content-Type": "application/json",
                    "User-Agent": "test-client",
                }
                req = Request(self.url, data=json.dumps(body).encode(), headers=headers)
                for _ in range(2):
                    try:
                        with urlopen(req, timeout=5) as response:
                            response.read()
                    except HTTPError as error:
                        error.close()

            def poll(self):
                return 0

        return patch("codex_runner.subprocess.Popen", FakeCLI)

    def test_cli_relay_caps_output_and_forwards_only_once(self):
        self.start(
            api_style="codex", mode="tokens", token_target=10000, token_limit=12000
        )
        with (
            patch("codex_runner.shutil.which", return_value="/fake/codex"),
            self.cli_fixture(),
        ):
            complete(self.book, self.book.reserve())
        self.assertEqual(len(self.server.calls), 1)
        path, body, auth = self.server.calls[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(auth, "Bearer fixture")
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["max_output_tokens"], 2048)
        self.assertEqual(self.book.snapshot()["totals"]["count"], 1)

    def test_cli_full_prompt_too_large_never_reaches_provider(self):
        self.start(
            api_style="codex", mode="tokens", token_target=10000, token_limit=12000
        )
        with (
            patch("codex_runner.shutil.which", return_value="/fake/codex"),
            self.cli_fixture(14000),
        ):
            complete(self.book, self.book.reserve())
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.book.snapshot()["held_tokens"], 0)
        self.assertFalse(self.book.snapshot()["enabled"])

    @unittest.skipUnless(
        shutil.which("codex"), "official CLI is tested in the container build"
    )
    def test_real_cli_with_local_provider(self):
        self.start(
            api_style="codex", mode="tokens", token_target=100000, token_limit=120000
        )
        complete(self.book, self.book.reserve(probe=False))
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.book.snapshot()["totals"]["count"], 1)

    def test_streamed_response_usage_and_missing_final_event(self):
        data = {
            "type": "response.incomplete",
            "response": {
                "usage": {
                    "input_tokens": 51,
                    "output_tokens": 32,
                    "input_tokens_details": {"cached_tokens": 0},
                }
            },
        }
        raw = (
            "event: response.incomplete\r\ndata: " + json.dumps(data) + "\r\n\r\n"
        ).encode()
        self.assertEqual(stream_usage(raw, "text/event-stream"), (51, 0, 32))
        with self.assertRaises(ValueError):
            stream_usage(b'data: {"type":"response.created"}\n\n', "text/event-stream")


if __name__ == "__main__":
    unittest.main()
