import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

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


if __name__ == "__main__":
    unittest.main()
