import fcntl
import hmac
import json
import os
import secrets
import signal
import sqlite3
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from auth import COOKIE, SESSION_TTL, Auth
from ledger import Ledger
from metering import number
from worker import Worker, complete

STATIC = Path(__file__).parent / "static"
ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/mark.svg": ("mark.svg", "image/svg+xml"),
    "/login.js": ("login.js", "text/javascript"),
    "/login.css": ("login.css", "text/css"),
}
# Files are immutable for the lifetime of a deployed process. Avoid rereading
# the container layer on every page load; private responses remain no-store.
ASSET_CONTENT = {
    path: ((STATIC / file).read_bytes(), mime) for path, (file, mime) in ASSETS.items()
}
LOGIN_PAGE = (STATIC / "login.html").read_text()


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenMeter/1"

    def log_message(self, fmt, *args):
        pass  # No credentials, query strings or provider responses in the access log.

    def reply(self, status, content=b"", mime="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, obj, status=200):
        self.reply(status, json.dumps(obj, ensure_ascii=False).encode())

    def authorized(self):
        self.session = self.server.auth.session(self.headers.get("Cookie"))
        if self.session:
            return True
        self.send_json({"error": "登录已过期，请重新登录"}, 401)
        return False

    def login_page(self, message="", status=200):
        content = LOGIN_PAGE.replace("{{csrf}}", self.server.auth.login_csrf)
        content = content.replace("{{message}}", escape(message))
        return self.reply(status, content.encode(), "text/html")

    def cookie(self, token=""):
        value = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL if token else 0}"
        # TLS terminates at the reverse proxy, which must overwrite this header.
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            value += "; Secure"
        return value

    def same_origin(self):
        origin = self.headers.get("Origin")
        return self.headers.get("Sec-Fetch-Site") != "cross-site" and (
            not origin or urlsplit(origin).netloc == self.headers.get("Host")
        )

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/healthz":
            return self.send_json({"ok": True})
        if parts.path in {"/", "/login"}:
            session = self.server.auth.session(self.headers.get("Cookie"))
            if not session:
                if parts.path == "/":
                    return self.reply(303, headers={"Location": "/login"})
                return self.login_page()
            if parts.path == "/login":
                return self.reply(303, headers={"Location": "/"})
        if parts.path in ASSET_CONTENT:
            content, mime = ASSET_CONTENT[parts.path]
            return self.reply(200, content, mime)
        if not self.authorized():
            return
        if parts.path == "/api/state":
            return self.send_json(
                {**self.server.book.snapshot(), "csrf_token": self.session["csrf"]}
            )
        if parts.path == "/api/logs":
            try:
                query = parse_qs(parts.query)
                before = number(query.get("before", [0])[0], "游标", 0, 2**63 - 1)
                return self.send_json(
                    self.server.book.logs(
                        before, estimated=query.get("estimated") == ["1"]
                    )
                )
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
        self.send_json({"error": "资源不存在"}, 404)

    def do_POST(self):
        self.connection.settimeout(10)
        if not self.same_origin():
            return self.send_json({"error": "请求来源无效"}, 403)
        if self.path == "/api/login":
            return self.login()
        if not self.authorized():
            return
        if not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", "").encode(), self.session["csrf"].encode()
        ):
            return self.send_json({"error": "页面令牌已失效，请刷新后重试"}, 403)
        if self.headers.get_content_type() != "application/json":
            return self.send_json({"error": "需要 JSON 请求"}, 415)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 16384:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise TypeError("请求格式无效")
            book = self.server.book
            if self.path == "/api/logout":
                self.server.auth.logout(self.headers.get("Cookie"))
                return self.reply(
                    200, b'{"ok":true}', headers={"Set-Cookie": self.cookie()}
                )
            elif self.path == "/api/settings":
                book.save(data)
            elif self.path == "/api/probe":
                job = book.reserve(probe=True)
                if not job:
                    raise ValueError("本周已达目标或剩余预算不足，无法进行计费测试")
                complete(book, job)
                row = book.logs(limit=1)["rows"][0]
                if row["status"] != "done":
                    raise ValueError(row["note"])
                return self.send_json(
                    {
                        "ok": True,
                        "prompt": row["prompt"],
                        "output": row["output"],
                        "cached": row["cached"],
                        "warning": row["note"],
                    }
                )
            elif self.path in {"/api/start", "/api/pause"}:
                book.switch(self.path == "/api/start")
            elif self.path == "/api/bark-test":
                book.test_notice()
            else:
                return self.send_json({"error": "资源不存在"}, 404)
            self.send_json({"ok": True})
        except (TypeError, ValueError, UnicodeError) as err:
            self.send_json({"error": str(err)}, 400)
        except (sqlite3.Error, OSError):
            self.send_json({"error": "保存失败，请检查数据卷空间和权限后重试"}, 503)

    def login(self):
        is_json = self.headers.get_content_type() == "application/json"
        try:
            if self.headers.get_content_type() not in {
                "application/json",
                "application/x-www-form-urlencoded",
            }:
                return self.send_json({"error": "请求格式无效"}, 415)
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 16384:
                raise ValueError("请求大小无效")
            raw = self.rfile.read(size).decode()
            data = (
                json.loads(raw)
                if is_json
                else {key: values[0] for key, values in parse_qs(raw).items()}
            )
            if not isinstance(data, dict):
                raise TypeError("请求格式无效")
            csrf = self.headers.get("X-CSRF-Token", data.get("csrf_token", ""))
            if not isinstance(csrf, str) or not hmac.compare_digest(
                csrf.encode(), self.server.auth.login_csrf.encode()
            ):
                message, status = "页面已过期，请刷新后重新登录", 403
            else:
                username, password = data.get("username"), data.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("请填写用户名和管理密码")
                token, status = self.server.auth.login(
                    username,
                    password,
                    self.client_address[0],
                    self.headers.get("Cookie"),
                )
                if token:
                    headers = {"Set-Cookie": self.cookie(token)}
                    if is_json:
                        return self.reply(200, b'{"ok":true}', headers=headers)
                    return self.reply(303, headers={**headers, "Location": "/"})
                message = (
                    "登录尝试过多，请一分钟后重试"
                    if status == 429
                    else "用户名或密码不正确，请重试"
                )
        except (TypeError, ValueError, UnicodeError):
            message, status = "请求格式无效，请重新填写", 400
        if is_json:
            return self.send_json({"error": message}, status)
        return self.login_page(message, status)


def main():
    os.umask(0o077)
    directory = Path(os.environ.get("DATA_DIR", "data"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "instance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        password_file = directory / "admin-password"
        password = os.getenv("ADMIN_PASSWORD")
        if not password:
            if not password_file.exists():
                password_file.write_text(secrets.token_urlsafe(24) + "\n")
            password = password_file.read_text().strip()
        if not password:
            raise ValueError("管理密码为空")
        book = Ledger(
            directory / "meter.sqlite3",
            os.getenv("BILLING_TZ", "Asia/Shanghai"),
            int(os.getenv("WEEK_START", "0")),
            log_days=int(os.getenv("REQUEST_LOG_DAYS", "14")),
            log_limit=int(os.getenv("REQUEST_LOG_MAX_ROWS", "50000")),
            storage_mode=os.getenv("STORAGE_MODE", "memory"),
            checkpoint_seconds=int(os.getenv("CHECKPOINT_SECONDS", "300")),
        )
        http = ThreadingHTTPServer(
            (os.getenv("BIND", "127.0.0.1"), int(os.getenv("PORT", "8080"))), Handler
        )
        http.auth = Auth(password)
        http.book = book
        worker = Worker(book)

        def shutdown(*_):
            worker.stop.set()
            threading.Thread(target=http.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        worker.start()
        print(f"Token Meter listening on port {http.server_port}", flush=True)
        try:
            http.serve_forever()
        finally:
            http.server_close()
            worker.close()
            book.close()


if __name__ == "__main__":
    main()
