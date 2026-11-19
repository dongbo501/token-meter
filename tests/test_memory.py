import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ledger import Ledger


class MemoryStorageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "meter.sqlite3"
        self.now = 1795104000
        self.book = self.open()
        self.addCleanup(lambda: self.book.db.close())
        self.book.save(
            {
                "base_url": "http://127.0.0.1:1",
                "api_key": "fixture",
                "model": "mock",
                "api_style": "chat",
                "mode": "tokens",
                "token_target": 1000000,
                "token_limit": 1100000,
                "input_price": 1,
                "output_price": 2,
                "cached_price": 0.25,
            }
        )
        self.book.switch(True)

    def open(self):
        return Ledger(self.path, storage_mode="memory", clock=lambda: self.now)

    def settle(self, count=1):
        for _ in range(count):
            job = self.book.reserve()
            self.assertIsNotNone(job)
            self.book.settle(job[0], 100, 10, 20)

    def test_requests_do_not_touch_disk_until_checkpoint_and_idle_flush_is_noop(self):
        before = self.path.read_bytes()
        modified = self.path.stat().st_mtime_ns
        self.settle(100)
        self.book.maintenance()
        self.book.flush()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, modified)
        self.now += 300
        self.book.flush()
        self.assertNotEqual(self.path.read_bytes(), before)
        modified = self.path.stat().st_mtime_ns
        self.now += 300
        self.book.flush()
        self.assertEqual(self.path.stat().st_mtime_ns, modified)

    def test_checkpoint_keeps_totals_samples_ids_but_excludes_detail_logs(self):
        self.settle(20)
        total = self.book.snapshot()["totals"]
        last = self.book.logs()["rows"][0]["id"]
        self.book.flush(force=True)
        with closing(sqlite3.connect(self.path)) as disk:
            self.assertEqual(
                disk.execute("SELECT count(*) FROM calls").fetchone()[0], 0
            )
            self.assertEqual(
                disk.execute("SELECT sum(count) FROM daily").fetchone()[0], 20
            )
            self.assertEqual(
                disk.execute("SELECT count(*) FROM success_samples").fetchone()[0], 1
            )
        self.assertEqual(len(self.book.logs()["rows"]), 20)
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["totals"], total)
        self.assertEqual(self.book.logs()["rows"], [])
        self.assertGreater(self.book.reserve()[0], last)

    def test_shutdown_saves_latest_summary(self):
        self.settle(3)
        self.book.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["totals"]["count"], 3)

    def test_reasoning_setting_is_saved_immediately_in_memory_mode(self):
        self.book.switch(False)
        self.book.save({"reasoning_effort": "xhigh"})
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["config"]["reasoning_effort"], "xhigh")

    def test_custom_timeout_is_saved_immediately_in_memory_mode(self):
        self.book.switch(False)
        self.book.save({"request_timeout": 900})
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["config"]["request_timeout"], 900)

    def test_crash_only_recovers_last_checkpoint_and_estimates_saved_active(self):
        self.settle(2)
        active = self.book.reserve()
        self.book.flush(force=True)
        self.book.settle(active[0], 100, 10, 20)
        self.settle(4)
        self.book.db.close()  # Simulate losing RAM without the shutdown checkpoint.
        self.book = self.open()
        state = self.book.snapshot()
        self.assertEqual(state["totals"]["count"], 3)
        self.assertEqual(state["totals"]["estimated_count"], 1)
        self.assertEqual(state["held_tokens"], 0)

    def test_settings_and_pause_are_saved_immediately(self):
        self.settle(2)
        self.book.switch(False)
        self.book.save({"model": "updated-model"})
        self.book.db.close()
        self.book = self.open()
        state = self.book.snapshot()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["totals"]["count"], 2)
        self.assertEqual(state["config"]["model"], "updated-model")

    def test_failed_checkpoint_keeps_previous_snapshot_and_pauses_dispatch(self):
        before = self.path.read_bytes()
        self.settle(2)
        connect = sqlite3.connect

        def fail_target(path, *args, **kwargs):
            if path == self.path:
                raise sqlite3.OperationalError("fixture disk full")
            return connect(path, *args, **kwargs)

        with (
            patch("ledger.sqlite3.connect", side_effect=fail_target),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.book.flush(force=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.book.snapshot()["enabled"])
        self.assertGreater(self.book.checkpoint_due, self.now)
        self.now += 61
        self.book.flush()
        self.book.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["totals"]["count"], 2)

    def test_existing_disk_ledger_can_migrate_to_memory_and_back(self):
        self.book.db.close()
        disk = Ledger(self.path)
        job = disk.reserve()
        disk.settle(job[0], 100, 10, 20)
        disk.db.close()
        self.book = self.open()
        self.assertEqual(self.book.snapshot()["totals"]["count"], 1)
        self.book.close()
        self.book = Ledger(self.path)
        self.assertEqual(self.book.snapshot()["totals"]["count"], 1)
        self.assertEqual(self.book.logs()["rows"], [])
