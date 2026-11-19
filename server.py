import base64
import fcntl
import hmac
import json
import os
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ledger import Ledger
from metering import number
from worker import Worker

STATIC = Path(__file__).parent / "static"
ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/mark.svg": ("mark.svg", "image/svg+xml"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenMeter/1"

    def log_message(self, fmt, *args):
        pass  # No credentials, query strings or provider responses in the access log.

    def reply(self, status, content=b"", mime="application/json", auth=False):
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
        if auth:
            self.send_header(
                "WWW-Authenticate", 'Basic realm="Token Meter", charset="UTF-8"'
            )
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, obj, status=200):
        self.reply(status, json.dumps(obj, ensure_ascii=False).encode())

    def authorized(self):
        if hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), self.server.credential
        ):
            return True
        self.reply(401, "请使用 admin 和管理密码登录".encode(), "text/plain", True)
        return False

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/healthz":
            return self.send_json({"ok": True})
        if not self.authorized():
            return
        if parts.path in ASSETS:
            file, mime = ASSETS[parts.path]
            return self.reply(200, (STATIC / file).read_bytes(), mime)
        if parts.path == "/api/state":
            return self.send_json(self.server.book.snapshot())
        if parts.path == "/api/logs":
            try:
                query = parse_qs(parts.query)
                before = number(query.get("before", [0])[0], "游标", 0, 2**63 - 1)
                return self.send_json(
                    self.server.book.logs(before, query.get("unknown") == ["1"])
                )
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
        self.send_json({"error": "资源不存在"}, 404)

    def do_POST(self):
        if not self.authorized():
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send_json({"error": "请求来源无效"}, 403)
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
            if self.path == "/api/settings":
                book.save(data)
            elif self.path in {"/api/start", "/api/pause"}:
                book.switch(self.path == "/api/start")
            elif self.path == "/api/bark-test":
                book.test_notice()
            elif self.path == "/api/resolve":
                ident = number(data.get("id"), "请求 ID", 1, 2**63 - 1)
                prompt = number(data.get("prompt"), "输入用量", 0, 10**8)
                cached = number(data.get("cached"), "缓存用量", 0, prompt)
                output = number(data.get("output"), "输出用量", 0, 10**8)
                book.settle(ident, prompt, cached, output, manual=True)
            else:
                return self.send_json({"error": "资源不存在"}, 404)
            self.send_json({"ok": True})
        except (TypeError, ValueError, UnicodeError) as err:
            self.send_json({"error": str(err)}, 400)


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
        )
        http = ThreadingHTTPServer(
            (os.getenv("BIND", "127.0.0.1"), int(os.getenv("PORT", "8080"))), Handler
        )
        http.credential = b"Basic " + base64.b64encode(("admin:" + password).encode())
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
            book.db.close()


if __name__ == "__main__":
    main()
