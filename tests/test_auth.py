import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from auth import SESSION_TTL, Auth
from ledger import Ledger
from server import Handler


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.auth = Auth("管理密码-fixture", clock=lambda: self.now)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.book = Ledger(Path(tmp.name) / "meter.sqlite3")
        self.addCleanup(self.book.db.close)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.auth, self.http.book = self.auth, self.book
        self.thread = threading.Thread(
            target=self.http.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.http.shutdown)

    def request(self, path, data=None, cookie=None, headers=None, method=None):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        conn = HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        try:
            conn.request(
                method or ("POST" if data is not None else "GET"), path, data, headers
            )
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login(self, **kwargs):
        data = {
            "username": "admin",
            "password": "管理密码-fixture",
            "csrf_token": self.auth.login_csrf,
        }
        return self.request("/api/login", data, **kwargs)

    def test_public_login_and_protected_dashboard_without_browser_prompt(self):
        status, headers, _ = self.request("/")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        status, _, body = self.request("/login")
        self.assertEqual(status, 200)
        self.assertIn(self.auth.login_csrf.encode(), body)
        self.assertIn(b'autocomplete="current-password"', body)
        for path in ("/api/state", "/api/logs"):
            status, headers, _ = self.request(path)
            self.assertEqual(status, 401)
            self.assertNotIn("WWW-Authenticate", headers)
        for path in ("/healthz", "/login.js", "/login.css", "/mark.svg"):
            self.assertEqual(self.request(path)[0], 200)

    def test_login_session_csrf_and_logout_revocation(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(self.request("/", cookie=cookie)[0], 200)
        state = json.loads(self.request("/api/state", cookie=cookie)[2])
        self.assertNotIn("管理密码", json.dumps(state))
        self.assertEqual(self.request("/api/pause", {}, cookie=cookie)[0], 403)
        csrf = {"X-CSRF-Token": state["csrf_token"]}
        self.assertEqual(
            self.request("/api/pause", {}, cookie=cookie, headers=csrf)[0], 200
        )
        status, headers, _ = self.request(
            "/api/logout", {}, cookie=cookie, headers=csrf
        )
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/state", cookie=cookie)[0], 401)

    def test_session_expiry_rotation_and_per_session_csrf(self):
        cookie = self.login()[1]["Set-Cookie"]
        other = self.login()[1]["Set-Cookie"]
        csrf = json.loads(self.request("/api/state", cookie=cookie)[2])["csrf_token"]
        self.assertEqual(
            self.request(
                "/api/pause", {}, cookie=other, headers={"X-CSRF-Token": csrf}
            )[0],
            403,
        )
        rotated = self.login(cookie=cookie)[1]["Set-Cookie"]
        self.assertEqual(self.request("/api/state", cookie=cookie)[0], 401)
        self.assertEqual(self.request("/api/state", cookie=rotated)[0], 200)
        self.now += SESSION_TTL
        self.assertEqual(self.request("/api/state", cookie=rotated)[0], 401)

    def test_login_rejects_csrf_cross_site_and_malformed_body(self):
        self.assertEqual(self.login(headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.login(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(
            self.request(
                "/api/login", {"username": "admin", "password": "管理密码-fixture"}
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "/api/login", b"[]", headers={"Content-Type": "application/json"}
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "/api/login", {"csrf_token": self.auth.login_csrf, "password": []}
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "/api/login", b"x" * 17000, headers={"Content-Type": "application/json"}
            )[0],
            400,
        )

    def test_form_submission_works_without_javascript_and_https_sets_secure(self):
        data = urlencode(
            {
                "username": "admin",
                "password": "管理密码-fixture",
                "csrf_token": self.auth.login_csrf,
            }
        )
        status, headers, _ = self.request(
            "/api/login",
            data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_bad_password_throttled_and_recovers_after_window(self):
        bad = {
            "username": "admin",
            "password": "wrong",
            "csrf_token": self.auth.login_csrf,
        }
        for _ in range(10):
            self.assertEqual(self.request("/api/login", bad)[0], 401)
        self.assertEqual(self.login()[0], 429)
        self.now += 61
        self.assertEqual(self.login()[0], 200)
