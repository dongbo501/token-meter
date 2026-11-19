import json
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from codex_runner import read_response, stream_usage
from ledger import Ledger
from metering import endpoint, usage
from worker import complete, deliver, post


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

    def test_repeated_failures_always_wait_five_seconds(self):
        self.start()
        for code in (429, 503, 429, 503, 503, 429):
            with self.subTest(code=code):
                self.server.reply = code, {}
                complete(self.book, self.book.reserve())
                self.assertEqual(self.book._state()["retry"], self.now.timestamp() + 5)
                self.now += timedelta(seconds=4)
                self.assertIsNone(self.book.reserve())
                self.now += timedelta(seconds=1)

    def test_upgrade_shortens_existing_long_backoff(self):
        self.start()
        with self.book.db:
            self.book.db.execute(
                "UPDATE state SET retry=?,failures=6", (self.now.timestamp() + 300,)
            )
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book._state()["retry"], self.now.timestamp() + 5)
        self.assertIsNone(self.book.reserve())
        self.now += timedelta(seconds=5)
        self.assertIsNotNone(self.book.reserve())

    def test_missing_usage_uses_last_success_and_continues_silently(self):
        self.start()
        first = self.book.reserve()
        complete(self.book, first)
        actual = self.book.logs()["rows"][0]
        self.server.reply = 200, {"choices": []}
        job = self.book.reserve()
        complete(self.book, job)
        state = self.book.snapshot()
        estimated = state["recent"][0]
        self.assertEqual(estimated["status"], "estimated")
        for field in ("prompt", "cached", "output", "pico"):
            self.assertEqual(estimated[field], actual[field])
        self.assertIn(f"#{first[0]}", estimated["note"])
        self.assertEqual(state["unknown"], 0)
        self.assertEqual(state["held_tokens"], 0)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["pending_notices"], 0)
        self.assertEqual(state["totals"]["estimated_count"], 1)
        self.assertEqual(state["totals"]["pico"], actual["pico"] * 2)
        self.assertEqual(state["totals"]["cached"], actual["cached"])
        self.assertEqual(state["totals"]["reported_input"], actual["prompt"])
        self.assertEqual(self.book.logs(estimated=True)["rows"], [estimated])
        self.assertIsNone(self.book.reserve())
        self.now += timedelta(seconds=6)
        self.assertIsNotNone(self.book.reserve())
        with self.assertRaises(ValueError):
            self.book.settle(job[0], 40, 2, 10)

    def test_missing_usage_without_success_uses_reservation(self):
        self.start()
        self.server.reply = 200, {}
        job = self.book.reserve()
        held = self.book.snapshot()
        complete(self.book, job)
        state = self.book.snapshot()
        row = state["recent"][0]
        self.assertEqual(row["prompt"] + row["output"], held["held_tokens"])
        self.assertEqual(row["usd"], held["held_usd"])
        self.assertIn("预留", row["note"])
        self.assertTrue(state["enabled"])
        self.assertEqual(state["pending_notices"], 0)
        self.assertEqual(state["held_usd"], 0)
        self.assertEqual(state["totals"]["reported_input"], 0)
        self.assertEqual(
            self.book.db.execute("SELECT count(*) FROM success_samples").fetchone()[0],
            0,
        )

    def test_interrupted_job_is_estimated_in_original_week(self):
        self.start()
        job = self.book.reserve()
        self.book.db.close()
        self.now += timedelta(days=7)
        self.book = self.open()
        state = self.book.snapshot()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["status"], "backoff")
        self.assertEqual(state["recent"][0]["status"], "estimated")
        self.assertEqual(state["spent"], 0)
        self.assertEqual(state["pending_notices"], 0)
        self.now += timedelta(seconds=6)
        self.assertIsNotNone(self.book.reserve())
        old = self.book.db.execute(
            "SELECT * FROM calls WHERE id=?", (job[0],)
        ).fetchone()
        self.assertEqual(self.book._summary(old["week"])["estimated_count"], 1)

    def test_upgrade_resolves_old_unknown_rows_and_discards_queued_errors(self):
        self.start()
        complete(self.book, self.book.reserve())
        job = self.book.reserve()
        with self.book.db:
            self.book.db.execute(
                "UPDATE calls SET status='unknown' WHERE id=?", (job[0],)
            )
            self.book._pause("请在请求记录中核对账单")
            self.book._notice(f"error/{job[0]}", "请求出错", "请核对")
            self.book.db.execute("DELETE FROM success_samples")
        self.book.db.close()
        self.book = self.open()
        state = self.book.snapshot()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["unknown"], 0)
        self.assertEqual(state["recent"][0]["status"], "estimated")
        self.assertEqual(state["recent"][0]["pico"], state["recent"][1]["pico"])
        self.assertEqual(state["pending_notices"], 0)
        self.book.switch(True)
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["totals"]["count"], 2)

    def test_repeated_errors_do_not_use_estimates_as_samples(self):
        self.start()
        first = self.book.reserve()
        complete(self.book, first)
        jobs = [self.book.reserve() for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda j: self.book.failed(j[0], "fixture timeout"), jobs))
        self.book.failed(jobs[0][0], "duplicate callback")
        state = self.book.snapshot()
        self.assertEqual(state["totals"]["count"], 4)
        self.assertEqual(state["totals"]["estimated_count"], 3)
        self.assertEqual(state["pending_notices"], 0)
        self.assertTrue(state["enabled"])
        for row in self.book.logs(estimated=True)["rows"]:
            self.assertIn(f"#{first[0]}", row["note"])

    def test_automatic_estimate_does_not_override_manual_pause(self):
        self.start()
        job = self.book.reserve()
        self.book.switch(False)
        self.book.failed(job[0], "fixture timeout")
        self.assertFalse(self.book.snapshot()["enabled"])
        self.assertEqual(self.book.snapshot()["note"], "已手动暂停")

    def test_http_503_and_timeout_are_estimated_without_bark(self):
        self.start()
        self.server.reply = 503, {}
        complete(self.book, self.book.reserve())
        self.now += timedelta(seconds=6)
        job = self.book.reserve()
        with patch("worker.post", side_effect=TimeoutError):
            complete(self.book, job)
        state = self.book.snapshot()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["totals"]["estimated_count"], 2)
        self.assertEqual(state["pending_notices"], 0)

    def test_estimate_can_complete_goal_but_only_sends_completion_notice(self):
        self.start(usd_target=0.01, usd_limit=0.02, input_price=100, output_price=100)
        job = self.book.reserve()
        self.book.settle(job[0], 30, 0, 20)
        self.book.failed(self.book.reserve()[0], "fixture timeout")
        state = self.book.snapshot()
        self.assertEqual(state["status"], "reached")
        self.assertEqual(state["spent"], 0.01)
        self.assertEqual(state["pending_notices"], 1)
        row, _ = self.book.next_notice()
        self.assertEqual(row["title"], "本周用量目标已完成")
        self.assertIn("自动估算 1 笔", row["body"])

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

    def test_api_reasoning_effort_and_default_payloads(self):
        for style in ("chat", "responses"):
            for effort in (
                "none", "minimal", "low", "medium", "high", "xhigh", "max", "default"
            ):
                with self.subTest(style=style, effort=effort):
                    self.start(api_style=style, reasoning_effort=effort)
                    complete(self.book, self.book.reserve())
                    body = self.server.calls[-1][1]
                    if effort == "default":
                        self.assertNotIn("reasoning", body)
                        self.assertNotIn("reasoning_effort", body)
                    elif style == "responses":
                        self.assertEqual(body["reasoning"], {"effort": effort})
                        self.assertNotIn("reasoning_effort", body)
                    else:
                        self.assertEqual(body["reasoning_effort"], effort)
                        self.assertNotIn("reasoning", body)
                    self.assertEqual(self.book.logs()["rows"][0]["status"], "done")
                    self.book.switch(False)

    def test_reasoning_setting_validation_persistence_and_reset(self):
        self.book.save({"reasoning_effort": "high"})
        for value in ("invalid", "HIGH", 1, None, [], {}):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                self.book.save({"reasoning_effort": value})
        self.book.save({"concurrency": 2})
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["config"]["reasoning_effort"], "high")
        self.book.save({"reasoning_effort": "default"})
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["config"]["reasoning_effort"], "default")

    def test_reasoning_migration_preserves_existing_protocol_defaults(self):
        for style in ("chat", "responses", "codex"):
            with self.subTest(style=style):
                cfg = json.loads(self.book._state()["config"])
                cfg["api_style"] = style
                cfg.pop("reasoning_effort")
                with self.book.db:
                    self.book.db.execute("UPDATE state SET config=?", (json.dumps(cfg),))
                self.book.db.close()
                self.book = self.open()
                expected = "low" if style == "codex" else "default"
                self.assertEqual(
                    self.book.snapshot()["config"]["reasoning_effort"], expected
                )

    def test_probe_uses_saved_reasoning_and_keeps_small_output_cap(self):
        self.book.save({"api_style": "responses", "reasoning_effort": "high"})
        complete(self.book, self.book.reserve(probe=True))
        body = self.server.calls[-1][1]
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["max_output_tokens"], 32)
        self.assertFalse(self.book.snapshot()["enabled"])

    def test_request_timeout_validation_migration_and_persistence(self):
        self.assertEqual(self.book.snapshot()["config"]["request_timeout"], 600)
        for value in (29, 3601, 30.5, True, None, "nan", "inf", [], {}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.book.save({"request_timeout": value})
        for value in (30, 3600, "900"):
            self.book.save({"request_timeout": value})
            self.book.db.close()
            self.book = self.open()
            self.assertEqual(
                self.book.snapshot()["config"]["request_timeout"], int(value)
            )
        cfg = json.loads(self.book._state()["config"])
        cfg.pop("request_timeout")
        with self.book.db:
            self.book.db.execute("UPDATE state SET config=?", (json.dumps(cfg),))
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["config"]["request_timeout"], 600)

    def test_api_and_probe_use_saved_timeout(self):
        for style in ("chat", "responses"):
            for probe in (False, True):
                with self.subTest(style=style, probe=probe):
                    self.book.save({"api_style": style, "request_timeout": 900})
                    if not probe:
                        self.book.switch(True)
                    with patch("worker.post", wraps=post) as request:
                        complete(self.book, self.book.reserve(probe=probe))
                    self.assertEqual(request.call_args.kwargs["timeout"], 900)
                    self.assertEqual(self.book.logs()["rows"][0]["status"], "done")
                    self.book.switch(False)

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

    def test_retention_preserves_estimated_totals_and_last_real_sample(self):
        self.start()
        first = self.book.reserve()
        complete(self.book, first)
        self.book.failed(self.book.reserve()[0], "fixture timeout")
        original = self.book.snapshot()["totals"]
        with self.book.db:
            self.book.db.execute("UPDATE calls SET ts=ts-20*86400")
        self.book.maintenance(force=True)
        self.assertEqual(self.book.logs()["rows"], [])
        self.assertEqual(self.book.snapshot()["totals"], original)
        self.now += timedelta(seconds=6)
        self.book.failed(self.book.reserve()[0], "another timeout")
        row = self.book.logs()["rows"][0]
        self.assertIn(f"#{first[0]}", row["note"])
        self.assertEqual(row["pico"], original["pico"] // 2)

    def test_log_cap_and_request_ids_survive_pruning(self):
        self.start()
        ids = []
        for _ in range(6):
            job = self.book.reserve()
            ids.append(job[0])
            complete(self.book, job)
        self.book.log_limit = 3
        self.book.maintenance(force=True)
        self.assertEqual(
            [row["id"] for row in self.book.logs()["rows"]], ids[-3:][::-1]
        )
        self.assertEqual(self.book.snapshot()["totals"]["count"], 6)
        self.now += timedelta(days=20)
        self.book.maintenance(force=True)
        self.assertEqual(self.book.logs()["rows"], [])
        self.assertGreater(self.book.reserve()[0], ids[-1])

    def test_pruning_reclaims_sqlite_pages(self):
        self.start()
        for _ in range(20):
            complete(self.book, self.book.reserve())
        with self.book.db:
            self.book.db.execute(
                "UPDATE calls SET note=?,ts=ts-20*86400", ("x" * 50000,)
            )
        self.book.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        before = self.path.stat().st_size
        self.book.maintenance(force=True)
        self.assertLess(self.path.stat().st_size, before)
        for _ in range(8):
            self.book.maintenance(force=True)
        self.assertLess(self.path.stat().st_size, before // 2)
        self.assertEqual(self.book.snapshot()["totals"]["count"], 20)

    def test_idle_polling_and_maintenance_do_not_write_or_truncate_wal(self):
        self.book.maintenance(force=True)
        wal = Path(str(self.path) + "-wal")
        before = wal.read_bytes()
        changes = self.book.db.total_changes
        for _ in range(20):
            self.book.reserve()
            self.book.snapshot()
            self.book.next_notice()
            self.book.maintenance(force=True)
        self.assertEqual(self.book.db.total_changes, changes)
        self.assertEqual(wal.read_bytes(), before)
        self.assertEqual(self.book.db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_maintenance_reclaims_at_most_256_pages_per_pass(self):
        self.start()
        for _ in range(20):
            complete(self.book, self.book.reserve())
        with self.book.db:
            self.book.db.execute("UPDATE calls SET note=?", ("x" * 200000,))
            self.book.db.execute("DELETE FROM calls")
        before = self.book.db.execute("PRAGMA freelist_count").fetchone()[0]
        self.assertGreater(before, 256)
        self.book.maintenance(force=True)
        after = self.book.db.execute("PRAGMA freelist_count").fetchone()[0]
        self.assertEqual(before - after, 256)

    def test_low_disk_pauses_schedule(self):
        self.start()
        space = type("Space", (), {"free": 64 * 1024**2})()
        with patch("ledger.shutil.disk_usage", return_value=space):
            self.book.maintenance(force=True)
            self.assertFalse(self.book.snapshot()["enabled"])
            with self.assertRaises(ValueError):
                self.book.switch(True)
        self.assertIn("磁盘空间", self.book.snapshot()["note"])

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

    def cli_fixture(self, input_size=4000, effort="default", request_timeout=600):
        test = self

        class FakeCLI:
            def __init__(self, args, env, **kwargs):
                self.env = env
                test.assertNotIn("fixture", json.dumps(env))
                test.assertEqual(Path(env["TMPDIR"]), Path(env["CODEX_HOME"]).parent)
                test.assertTrue(Path(env["TMPDIR"]).is_dir())
                test.assertIn('history.persistence="none"', args)
                test.assertIn(
                    f"model_providers.meter.stream_idle_timeout_ms={(request_timeout + 10) * 1000}",
                    args,
                )
                effort_args = [a for a in args if a.startswith("model_reasoning_effort=")]
                test.assertEqual(
                    effort_args,
                    []
                    if effort == "default"
                    else [f"model_reasoning_effort={json.dumps(effort)}"],
                )
                value = next(
                    a for a in args if a.startswith("model_providers.meter.base_url=")
                )
                self.url = json.loads(value.split("=", 1)[1]) + "/responses"

            def communicate(self, _prompt, timeout):
                test.assertGreater(timeout, request_timeout)
                test.assertLessEqual(timeout, request_timeout + 20)
                body = {
                    "model": "mock",
                    "instructions": "x" * input_size,
                    "input": "Return Python code",
                    "tools": [{"type": "function"}],
                    "reasoning": {"effort": "medium" if effort == "default" else effort},
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

    def test_cli_reasoning_options_reach_provider(self):
        for effort in (
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "default"
        ):
            with self.subTest(effort=effort):
                self.start(api_style="codex", reasoning_effort=effort)
                with (
                    patch("codex_runner.shutil.which", return_value="/fake/codex"),
                    self.cli_fixture(effort=effort),
                ):
                    complete(self.book, self.book.reserve())
                body = self.server.calls[-1][1]
                expected = "medium" if effort == "default" else effort
                self.assertEqual(body["reasoning"], {"effort": expected})
                self.assertEqual(body["max_output_tokens"], 2048)
                self.assertEqual(self.book.logs()["rows"][0]["status"], "done")
                self.book.switch(False)

    def test_cli_custom_timeout_reaches_relay_and_process(self):
        self.start(api_style="codex", request_timeout=900)
        with (
            patch("codex_runner.shutil.which", return_value="/fake/codex"),
            self.cli_fixture(request_timeout=900),
            patch("codex_runner.read_response", wraps=read_response) as read,
        ):
            started = time.monotonic()
            complete(self.book, self.book.reserve())
            deadline = read.call_args.args[2]
            self.assertGreaterEqual(deadline, started + 900)
            self.assertLessEqual(deadline, time.monotonic() + 900)
        self.assertEqual(self.book.logs()["rows"][0]["status"], "done")

    @unittest.skipUnless(
        shutil.which("codex"), "official CLI is tested in the container build"
    )
    def test_real_cli_with_local_provider(self):
        self.start(
            api_style="codex",
            mode="tokens",
            token_target=100000,
            token_limit=120000,
            reasoning_effort="high",
        )
        complete(self.book, self.book.reserve(probe=False))
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.server.calls[0][1]["reasoning"]["effort"], "high")
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
