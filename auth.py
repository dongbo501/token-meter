"""Bounded, memory-only admin sessions; restarting the server signs users out."""

import hashlib
import hmac
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie

COOKIE = "meter_session"
SESSION_TTL = 12 * 3600


class Auth:
    def __init__(self, password, clock=time.monotonic):
        self.password = hashlib.sha256(password.encode()).digest()
        self.clock = clock
        self.login_csrf = secrets.token_urlsafe(32)
        self.sessions = {}
        self.attempts = {}
        self.lock = threading.Lock()

    @staticmethod
    def token(header):
        try:
            cookie = SimpleCookie(header or "")
            return cookie[COOKIE].value if COOKIE in cookie else ""
        except CookieError:
            return ""

    def session(self, header):
        with self.lock:
            token = self.token(header)
            session = self.sessions.get(token)
            if session and session["expires"] > self.clock():
                return session
            self.sessions.pop(token, None)

    def login(self, username, password, peer, old_cookie=None):
        with self.lock:
            now = self.clock()
            self.attempts = {
                key: value for key, value in self.attempts.items() if value[1] > now
            }
            count, reset = self.attempts.get(peer, (0, now + 60))
            if count >= 10 or (
                peer not in self.attempts and len(self.attempts) >= 1024
            ):
                return None, 429
            self.attempts[peer] = (count + 1, reset)
            valid = hmac.compare_digest(
                hashlib.sha256(password.encode()).digest(), self.password
            )
            if not valid or username != "admin":
                return None, 401
            self.attempts.pop(peer, None)
            self.sessions = {
                key: value
                for key, value in self.sessions.items()
                if value["expires"] > now
            }
            self.sessions.pop(self.token(old_cookie), None)
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {
                "expires": now + SESSION_TTL,
                "csrf": secrets.token_urlsafe(32),
            }
            return token, 200

    def logout(self, header):
        with self.lock:
            self.sessions.pop(self.token(header), None)
