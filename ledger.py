"""Persistent weekly reservations. All state transitions commit under the same lock."""

import json
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from metering import (
    DEFAULTS,
    PICO,
    allocation,
    configure,
    cost,
    dollars,
    prices,
    week_of,
    workload,
)


class Ledger:
    def __init__(
        self,
        path,
        timezone="Asia/Shanghai",
        start_day=0,
        clock=time.time,
        log_days=14,
        log_limit=50000,
    ):
        if not 1 <= log_days <= 3650 or not 100 <= log_limit <= 1000000:
            raise ValueError(
                "REQUEST_LOG_DAYS must be 1..3650; REQUEST_LOG_MAX_ROWS must be 100..1000000"
            )
        self.path = Path(path)
        self.log_days, self.log_limit = log_days, log_limit
        self.maintenance_due = 0
        self.zone, self.start_day, self.clock = ZoneInfo(timezone), start_day, clock
        if not 0 <= start_day <= 6:
            raise ValueError("WEEK_START must be 0..6")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Enable physical reclamation on new databases and migrate old ones only
        # when there is enough free space for SQLite's one-time rebuild.
        if self.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0:
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            size = self.path.stat().st_size
            if (
                size
                and shutil.disk_usage(self.path.parent).free > size * 2 + 128 * 1024**2
            ):
                self.db.execute("VACUUM")
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL,
            enabled INTEGER DEFAULT 0, reason TEXT DEFAULT '', retry REAL DEFAULT 0, failures INTEGER DEFAULT 0, tail_week TEXT DEFAULT '');
          CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, ts REAL NOT NULL, week TEXT NOT NULL,
            day TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, reserved_t INTEGER, reserved_p INTEGER,
            rates TEXT NOT NULL, prompt INTEGER, cached INTEGER, output INTEGER, pico INTEGER, note TEXT DEFAULT '');
          CREATE INDEX IF NOT EXISTS calls_status ON calls(status,week);
          CREATE INDEX IF NOT EXISTS calls_week ON calls(week,id);
          CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY, event TEXT UNIQUE, title TEXT, body TEXT,
            created REAL, delivered REAL, attempts INTEGER DEFAULT 0, retry REAL DEFAULT 0, error TEXT DEFAULT '');
          CREATE INDEX IF NOT EXISTS pending_notices ON notices(retry,id) WHERE delivered IS NULL;
          CREATE TABLE IF NOT EXISTS daily (week TEXT, day TEXT, prompt INTEGER, cached INTEGER,
            reported_input INTEGER, output INTEGER, pico INTEGER, count INTEGER, PRIMARY KEY(week,day));
        """)
        with self.db:
            state_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(state)")
            }
            if "next_call_id" not in state_columns:
                self.db.execute(
                    "ALTER TABLE state ADD COLUMN next_call_id INTEGER NOT NULL DEFAULT 1"
                )
                self.db.execute(
                    "UPDATE state SET next_call_id=(SELECT coalesce(max(id),0)+1 FROM calls)"
                )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(calls)")}
            if "reserved_output" not in columns:
                self.db.execute("ALTER TABLE calls ADD COLUMN reserved_output INTEGER")
            self.db.execute(
                "INSERT OR IGNORE INTO state(id,config) VALUES(1,?)",
                (json.dumps(DEFAULTS),),
            )
            # Preserve the protocol of existing deployments during schema migration.
            saved = json.loads(self._state()["config"])
            if "api_style" not in saved:
                saved["api_style"] = "chat"
                self.db.execute(
                    "UPDATE state SET config=? WHERE id=1", (json.dumps(saved),)
                )
            interrupted_ids = self.db.execute(
                "SELECT id FROM calls WHERE status='active'"
            ).fetchall()
            interrupted = self.db.execute(
                "UPDATE calls SET status='unknown',note='服务在请求结算前重启' WHERE status='active'"
            )
            if interrupted.rowcount:
                self._pause("存在中断的请求，请在请求记录中核对账单")
                for row in interrupted_ids:
                    self._notice(
                        f"error/{row['id']}",
                        "请求中断",
                        f"请求 #{row['id']} 在结算前重启，请核对供应商账单。",
                    )

    def maintenance(self, force=False):
        """Bound details independently of the durable daily budget aggregates."""
        with self.lock:
            now = self.clock()
            if not force and now < self.maintenance_due:
                return
            with self.db:
                self.db.execute(
                    "DELETE FROM calls WHERE status IN ('done','failed') AND ts<?",
                    (now - self.log_days * 86400,),
                )
                self.db.execute(
                    """DELETE FROM calls WHERE id IN (SELECT id FROM calls
                    WHERE status IN ('done','failed') ORDER BY id DESC LIMIT -1 OFFSET ?)""",
                    (self.log_limit,),
                )
                goal_prefix = f"goal/{self._window(now)}/%"
                self.db.execute(
                    """DELETE FROM notices WHERE event NOT LIKE ? AND
                    (created<? OR id IN (SELECT id FROM notices ORDER BY id DESC LIMIT -1 OFFSET 10000))""",
                    (goal_prefix, now - 30 * 86400),
                )
                if shutil.disk_usage(self.path.parent).free < 128 * 1024**2:
                    self._pause(
                        "数据卷剩余空间不足 128 MiB，计划已暂停，请释放磁盘空间"
                    )
                    self._notice(
                        f"disk/{self._window(now)}",
                        "磁盘空间不足",
                        "数据卷剩余空间不足 128 MiB，计划已暂停。",
                    )
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            self.db.execute("PRAGMA incremental_vacuum").fetchall()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            self.maintenance_due = now + 300

    def storage_bytes(self):
        return sum(
            p.stat().st_size
            for p in (self.path, Path(str(self.path) + "-wal"))
            if p.exists()
        )

    def _notice(self, event, title, body):
        if json.loads(self._state()["config"]).get("bark_url"):
            self.db.execute(
                "INSERT OR IGNORE INTO notices(event,title,body,created) VALUES(?,?,?,?)",
                (event, title, body, self.clock()),
            )

    def _goal_notice(self, week, cfg):
        target, _, spent = self._limits(cfg, self._summary(week))
        if spent >= target:
            usd = cfg["mode"] == "usd"
            amount = (
                f"${spent / PICO:,.4f} / ${target / PICO:,.2f}"
                if usd
                else f"{spent:,} / {target:,} tokens"
            )
            self._notice(
                f"goal/{week}/{cfg['mode']}/{target}",
                "本周用量目标已完成",
                f"周期 {week} · 已结算 {amount}",
            )

    def test_notice(self):
        with self.lock, self.db:
            if not json.loads(self._state()["config"]).get("bark_url"):
                raise ValueError("请先保存 Bark 推送地址")
            self._notice(
                f"test/{time.time_ns()}", "Bark 连接测试", "Token Meter 通知已送达。"
            )

    def next_notice(self):
        with self.lock:
            url = json.loads(self._state()["config"]).get("bark_url")
            if url:
                row = self.db.execute(
                    """SELECT * FROM notices WHERE delivered IS NULL AND retry<=?
                    ORDER BY id LIMIT 1""",
                    (self.clock(),),
                ).fetchone()
                if row:
                    return dict(row), url

    def notice_result(self, row, error=""):
        with self.lock, self.db:
            now = self.clock()
            retry = now + min(3600, 5 * 2 ** min(10, row["attempts"]))
            self.db.execute(
                "UPDATE notices SET delivered=?,attempts=attempts+1,retry=?,error=? WHERE id=? AND event=?",
                (None if error else now, retry, error, row["id"], row["event"]),
            )

    def _state(self):
        return self.db.execute("SELECT * FROM state WHERE id=1").fetchone()

    def _pause(self, note):
        self.db.execute(
            "UPDATE state SET enabled=0,reason=?,retry=0,failures=0", (note,)
        )

    def save(self, changes):
        with self.lock, self.db:
            s = self._state()
            if (
                s["enabled"]
                or self.db.execute(
                    "SELECT 1 FROM calls WHERE status='active' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("请先暂停计划，待在途请求结算后保存设置")
            cfg = configure(json.loads(s["config"]), changes)
            if not cfg["bark_url"]:
                self.db.execute("DELETE FROM notices WHERE delivered IS NULL")
            self.db.execute("UPDATE state SET config=? WHERE id=1", (json.dumps(cfg),))

    def switch(self, enable):
        with self.lock, self.db:
            if enable:
                if shutil.disk_usage(self.path.parent).free < 128 * 1024**2:
                    raise ValueError("数据卷剩余空间不足 128 MiB，请释放磁盘空间")
                if self.db.execute(
                    "SELECT 1 FROM calls WHERE status='unknown' LIMIT 1"
                ).fetchone():
                    raise ValueError("请先在请求记录中核对待确认的用量")
                cfg = json.loads(self._state()["config"])
                configure(cfg, {})
                self.db.execute("UPDATE state SET enabled=1,reason='',tail_week=''")
                self._goal_notice(self._window(self.clock()), cfg)
            else:
                self._pause("已手动暂停")

    def _window(self, now):
        return week_of(now, self.zone, self.start_day).isoformat()

    def _summary(self, week):
        return self.db.execute(
            """SELECT coalesce(sum(prompt),0) AS prompt,coalesce(sum(output),0) AS output,
            coalesce(sum(pico),0) AS pico,coalesce(sum(cached),0) AS cached,coalesce(sum(count),0) AS count,
            coalesce(sum(reported_input),0) AS reported_input FROM daily WHERE week=?""",
            (week,),
        ).fetchone()

    def _held(self, week):
        return self.db.execute(
            """SELECT coalesce(sum(reserved_t),0),coalesce(sum(reserved_p),0)
            FROM calls WHERE week=? AND status IN ('active','unknown')""",
            (week,),
        ).fetchone()

    def _limits(self, cfg, totals):
        if cfg["mode"] == "tokens":
            return (
                cfg["token_target"],
                cfg["token_limit"],
                totals["prompt"] + totals["output"],
            )
        return dollars(cfg["usd_target"]), dollars(cfg["usd_limit"]), totals["pico"]

    def reserve(self, probe=False):
        with self.lock, self.db:
            s, now = self._state(), self.clock()
            if not probe and (not s["enabled"] or now < s["retry"]):
                return
            if probe:
                if (
                    s["enabled"]
                    or self.db.execute(
                        "SELECT 1 FROM calls WHERE status IN ('active','unknown') LIMIT 1"
                    ).fetchone()
                ):
                    raise ValueError("请先暂停计划，等待在途请求完成并核对未知用量")
                configure(json.loads(s["config"]), {})
            week = self._window(now)
            if s["tail_week"] == week:
                return
            cfg = json.loads(s["config"])
            if probe:
                cfg["max_output"] = 32
            active = self.db.execute(
                "SELECT count(*) FROM calls WHERE status='active'"
            ).fetchone()[0]
            if active >= cfg["concurrency"]:
                return
            held_tokens, held_pico = self._held(week)
            held = held_pico if cfg["mode"] == "usd" else held_tokens
            target, cap, spent = self._limits(cfg, self._summary(week))
            if spent + held >= target:
                return
            prompt = workload(cfg["api_style"])
            if probe:
                prompt = (
                    prompt.split(" | ", 1)[0]
                    + " Connection test. Reply exactly OK. Do not use tools."
                )
            rates = prices(cfg)
            quote = allocation(cfg, prompt, cap - spent - held, rates)
            if not quote:
                # Try a shorter, still unique math task near the end of the budget.
                prompt = (
                    prompt.split(" | ", 1)[0]
                    + " How many distinct hex digits appear in the ID? Explain."
                )
                quote = allocation(cfg, prompt, cap - spent - held, rates)
            if not quote:
                if not active:
                    self.db.execute(
                        "UPDATE state SET reason=?,tail_week=?",
                        ("余额不足以预留下次请求", week),
                    )
                    self._notice(
                        f"tail/{week}/{cfg['mode']}/{cap}/{target}",
                        "本周预算余量不足",
                        f"周期 {week} · 尚未达到目标，剩余额度不足以派发请求。可在面板调整上限。",
                    )
                return
            out, tokens, pico = quote
            day = datetime.fromtimestamp(now, self.zone).date().isoformat()
            cur = self.db.execute(
                """INSERT INTO calls(id,ts,week,day,model,status,reserved_t,reserved_p,rates,reserved_output)
                VALUES(?,?,?,?,?,'active',?,?,?,?)""",
                (
                    s["next_call_id"],
                    now,
                    week,
                    day,
                    cfg["model"],
                    tokens,
                    pico,
                    json.dumps(rates),
                    out,
                ),
            )
            self.db.execute(
                "UPDATE state SET reason='',tail_week='',next_call_id=next_call_id+1"
            )
            return cur.lastrowid, cfg, prompt, out

    def resize(self, ident, cfg, request_text):
        """Reserve the complete CLI request under the same lock before network I/O."""
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM calls WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != "active":
                raise ValueError("请求已结束")
            _, cap, spent = self._limits(cfg, self._summary(row["week"]))
            ht, hp = self._held(row["week"])
            held = (
                hp - row["reserved_p"]
                if cfg["mode"] == "usd"
                else ht - row["reserved_t"]
            )
            quote = allocation(cfg, request_text, cap - spent - held, prices(cfg))
            if not quote:
                raise ValueError("剩余预算不足以容纳 Codex 的完整提示词，请调整上限")
            out, tokens, pico = quote
            self.db.execute(
                "UPDATE calls SET reserved_t=?,reserved_p=?,reserved_output=? WHERE id=?",
                (tokens, pico, out, ident),
            )
            return out

    def settle(self, ident, prompt, cached, output, manual=False):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM calls WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != ("unknown" if manual else "active"):
                raise ValueError("该请求的结算状态已改变，请刷新")
            pico = cost(prompt, cached, output, json.loads(row["rates"]))
            self.db.execute(
                """UPDATE calls SET status='done',prompt=?,cached=?,output=?,pico=?,note=? WHERE id=?""",
                (prompt, cached, output, pico, "已按账单核对" if manual else "", ident),
            )
            self.db.execute(
                """INSERT INTO daily VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(week,day) DO UPDATE SET
                prompt=prompt+excluded.prompt,cached=cached+excluded.cached,reported_input=reported_input+excluded.reported_input,
                output=output+excluded.output,pico=pico+excluded.pico,count=count+1""",
                (
                    row["week"],
                    row["day"],
                    prompt,
                    cached or 0,
                    prompt if cached is not None else 0,
                    output,
                    pico,
                ),
            )
            self._goal_notice(row["week"], json.loads(self._state()["config"]))
            output_overrun = (
                row["reserved_output"] is not None and output > row["reserved_output"]
            )
            if (
                prompt + output > row["reserved_t"]
                or pico > row["reserved_p"]
                or output_overrun
            ):
                note = (
                    "供应商未遵守单次输出上限，计划已暂停，请核对计量方式"
                    if output_overrun
                    else "实际用量超过请求预留，请检查供应商的计量方式"
                )
                self._pause(note)
                self.db.execute("UPDATE calls SET note=? WHERE id=?", (note, ident))
                self._notice(
                    f"over/{ident}",
                    "请求用量超过预留",
                    f"请求 #{ident} 的实际用量超过预留，计划已暂停。",
                )
            elif not manual:
                self.db.execute(
                    "UPDATE state SET failures=0 WHERE retry<=?", (self.clock(),)
                )
            elif not self.db.execute(
                "SELECT 1 FROM calls WHERE status='unknown' LIMIT 1"
            ).fetchone():
                self._pause("账单已核对，可重新启动计划")

    def failed(self, ident, note, kind="unknown", delay=0):
        with self.lock, self.db:
            status = "unknown" if kind == "unknown" else "failed"
            changed = self.db.execute(
                "UPDATE calls SET status=?,note=? WHERE id=? AND status='active'",
                (status, note, ident),
            )
            if not changed.rowcount:
                return
            self._notice(f"error/{ident}", "接口请求出错", f"请求 #{ident} · {note}")
            if kind == "limited":
                s = self._state()
                seconds = max(delay, min(300, 5 * 2 ** min(s["failures"], 6)))
                self.db.execute(
                    "UPDATE state SET retry=max(retry,?), failures=failures+1",
                    (self.clock() + seconds,),
                )
            else:
                self._pause(note)

    def logs(self, before=0, unknown=False, limit=40):
        with self.lock:
            rows = self.db.execute(
                """SELECT id,ts,model,status,prompt,cached,output,pico,note FROM calls
                WHERE (?=0 OR id<?) AND (?=0 OR status='unknown') ORDER BY id DESC LIMIT ?""",
                (before, before, int(unknown), limit + 1),
            ).fetchall()
            items = [
                {
                    **dict(row),
                    "usd": row["pico"] / PICO if row["pico"] is not None else None,
                }
                for row in rows[:limit]
            ]
            return {"rows": items, "more": len(rows) > limit}

    def snapshot(self):
        with self.lock:
            s, now = self._state(), self.clock()
            cfg = json.loads(s["config"])
            ready = bool(cfg.pop("api_key"))
            has_bark = bool(cfg.pop("bark_url", ""))
            pending = self.db.execute(
                "SELECT count(*) FROM notices WHERE delivered IS NULL"
            ).fetchone()[0]
            last_notice = self.db.execute("""SELECT title,delivered,error FROM notices
                ORDER BY id DESC LIMIT 1""").fetchone()
            start = week_of(now, self.zone, self.start_day)
            week = str(start)
            totals = dict(self._summary(week))
            target, cap, spent = self._limits(cfg, totals)
            held_t, held_p = self._held(week)
            active, unknown = [
                self.db.execute(
                    "SELECT count(*) FROM calls WHERE status=?", (key,)
                ).fetchone()[0]
                for key in ("active", "unknown")
            ]
            daily = {
                r["day"]: r
                for r in self.db.execute("SELECT * FROM daily WHERE week=?", (week,))
            }
            days = []
            for index in range(7):
                day = str(start + timedelta(days=index))
                row = daily.get(day)
                days.append(
                    {
                        "day": day,
                        "tokens": row["prompt"] + row["output"] if row else 0,
                        "usd": row["pico"] / PICO if row else 0,
                    }
                )
            state = "paused"
            if unknown:
                state = "review"
            elif s["enabled"]:
                state = (
                    "reached"
                    if spent >= target
                    else "backoff"
                    if s["retry"] > now
                    else "tail"
                    if s["tail_week"] == week and not active
                    else "running"
                )
            elif not ready:
                state = "setup"
            scale = PICO if cfg["mode"] == "usd" else 1
            return {
                "storage": {
                    "bytes": self.storage_bytes(),
                    "log_days": self.log_days,
                    "log_limit": self.log_limit,
                },
                "config": cfg,
                "ready": ready,
                "has_bark": has_bark,
                "pending_notices": pending,
                "last_notice": dict(last_notice) if last_notice else None,
                "enabled": bool(s["enabled"]),
                "status": state,
                "note": s["reason"],
                "retry": s["retry"],
                "active": active,
                "unknown": unknown,
                "timezone": str(self.zone),
                "today": datetime.fromtimestamp(now, self.zone).date().isoformat(),
                "week": week,
                "next_week": str(start + timedelta(days=7)),
                "days": days,
                "target": target / scale,
                "cap": cap / scale,
                "spent": spent / scale,
                "held_tokens": held_t,
                "held_usd": held_p / PICO,
                "totals": {**totals, "usd": totals["pico"] / PICO},
                "recent": self.logs(limit=5)["rows"],
            }
