import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from codex_runner import LocalReject, run_codex
from http_client import read_chunks, remaining
from metering import endpoint, usage

logger = logging.getLogger(__name__)
UPSTREAM_ERRORS = (OSError, HTTPException, ValueError, TypeError, RecursionError)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(url, data, headers=None, timeout=600):
    deadline = time.monotonic() + timeout
    request = Request(
        url,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with build_opener(NoRedirect).open(request, timeout=remaining(deadline)) as response:
        raw = b"".join(read_chunks(response, deadline))
        return json.loads(raw)


def complete(book, job):
    ident, cfg, prompt, out = job
    try:
        if cfg["api_style"] == "codex":
            counts = run_codex(book, job)
        else:
            payload = {
                "model": cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
                cfg["output_field"]: out,
                "stream": False,
            }
            if cfg["api_style"] == "responses":
                payload = {
                    "model": cfg["model"],
                    "input": prompt,
                    "max_output_tokens": out,
                    "store": False,
                    "stream": False,
                }
            effort = cfg["reasoning_effort"]
            if effort != "default":
                if cfg["api_style"] == "responses":
                    payload["reasoning"] = {"effort": effort}
                else:
                    payload["reasoning_effort"] = effort
            data = post(
                endpoint(cfg["base_url"], cfg["api_style"]),
                payload,
                {"Authorization": "Bearer " + cfg["api_key"]},
                timeout=cfg["request_timeout"],
            )
            counts = usage(data.get("usage") if isinstance(data, dict) else None)
    except LocalReject as err:
        book.failed(ident, str(err), "rejected")
        return
    except HTTPError as err:
        code = err.code
        err.close()
        if code == 429:
            book.failed(ident, "HTTP 429 · 供应商限流，5 秒后重试", "limited")
        elif code in {400, 401, 402, 403, 404, 405, 413, 415, 422} or 300 <= code < 400:
            book.failed(
                ident,
                f"HTTP {code} · 请求被拒绝，请检查执行方式、接口、模型、参数和账户",
                "rejected",
            )
        else:
            book.failed(ident, f"HTTP {code} · 接口未返回有效用量")
        return
    except UPSTREAM_ERRORS as err:
        # Provider payloads and exception URLs may contain secrets. Store only a fixed category.
        note = (
            "响应缺少有效用量"
            if isinstance(err, (ValueError, TypeError, RecursionError))
            else f"连接中断或超时（请求上限 {cfg['request_timeout']} 秒）"
        )
        book.failed(ident, note)
        return
    book.settle(ident, *counts)


def deliver(book, notice):
    row, address = notice
    # Bark JSON API supports device_key and leaves title/body out of the URL.
    parts = urlsplit(address)
    prefix, _, key = parts.path.rstrip("/").rpartition("/")
    url = urlunsplit((parts.scheme, parts.netloc, prefix + "/push", "", ""))
    error = ""
    try:
        result = post(
            url,
            {
                "device_key": key,
                "title": row["title"],
                "body": row["body"],
                "group": "Token Meter",
            },
            timeout=12,
        )
        if not isinstance(result, dict) or result.get("code") != 200:
            error = "Bark 未确认投递，请检查设备 Key"
    except HTTPError as err:
        error = f"Bark 返回 HTTP {err.code}"
        err.close()
    except UPSTREAM_ERRORS:
        error = "Bark 连接失败或响应无效"
    book.notice_result(row, error)


class Worker:
    def __init__(self, book):
        self.book = book
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="provider")

    def run(self):
        while not self.stop.is_set():
            self.book.maintenance()
            try:
                self.book.flush()
            except (sqlite3.Error, OSError):
                # flush pauses dispatch and schedules a bounded retry; keep the
                # scheduler alive so recovery does not require a container restart.
                self.stop.wait(1)
                continue
            job = self.book.reserve()
            if job:
                fut = self.pool.submit(complete, self.book, job)
                fut.add_done_callback(self.check)
            else:
                self.stop.wait(0.35)

    @staticmethod
    def check(future):
        if future.exception():
            logger.error(
                "Unexpected ledger settlement error", exc_info=future.exception()
            )

    def notify(self):
        while not self.stop.is_set():
            notice = self.book.next_notice()
            if notice:
                deliver(self.book, notice)
            else:
                self.stop.wait(1)

    def start(self):
        self.threads = [
            threading.Thread(target=task, daemon=True)
            for task in (self.run, self.notify)
        ]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()
        self.pool.shutdown(wait=True)
