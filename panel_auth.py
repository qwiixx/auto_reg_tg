"""Single-operator authentication for the local panel."""

import math
import secrets
from hashlib import sha256
from time import monotonic

COOKIE_NAME = "reg_panel_auth"
SESSION_TTL = 12 * 60 * 60
MAX_CREDENTIAL_LENGTH = 512
MAX_LOGIN_FAILURES = 5
LOGIN_COOLDOWN = 30


def _digest(value: str) -> bytes:
    return sha256(value.encode("utf-8", errors="surrogatepass")).digest()


class PanelAuth:
    def __init__(self, username: str, password: str) -> None:
        if not username.strip() or len(username) > MAX_CREDENTIAL_LENGTH:
            raise ValueError("Укажите непустой PANEL_USERNAME длиной до 512 символов.")
        if not password.strip() or not 12 <= len(password) <= MAX_CREDENTIAL_LENGTH:
            raise ValueError("Задайте PANEL_PASSWORD длиной от 12 до 512 символов.")
        self._username = _digest(username)
        self._password = _digest(password)
        self._sessions: dict[str, float] = {}
        self._failures = 0
        self._blocked_until = 0.0

    def login(self, username: str, password: str) -> tuple[str | None, int]:
        now = monotonic()
        if now < self._blocked_until:
            return None, math.ceil(self._blocked_until - now)
        username_matches = secrets.compare_digest(self._username, _digest(username))
        password_matches = secrets.compare_digest(self._password, _digest(password))
        if not (username_matches and password_matches):
            self._failures += 1
            if self._failures == MAX_LOGIN_FAILURES:
                self._failures = 0
                self._blocked_until = now + LOGIN_COOLDOWN
                return None, LOGIN_COOLDOWN
            return None, 0

        self._failures = 0
        self._sessions = {token: expiry for token, expiry in self._sessions.items() if expiry > now}
        token = secrets.token_urlsafe(32)
        self._sessions[token] = now + SESSION_TTL
        return token, 0

    def authorized(self, token: str) -> bool:
        return self._sessions.get(token, 0.0) > monotonic()

    def logout(self, token: str) -> None:
        self._sessions.pop(token, None)
