"""Dashboard password. Only a salted PBKDF2 hash is stored (data/auth.json), never the password.
Sessions are random tokens in an HttpOnly cookie, valid 30 days."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from .config import settings

COOKIE = "radar_session"
SESSION_DAYS = 30
MAX_TRIES, LOCK_SECONDS = 5, 300
_ITER = 300_000
_fails: list[float] = []


def _file():
    return settings.data_dir / "auth.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    f = _file()
    f.write_text(json.dumps(d))
    try:
        f.chmod(0o600)
    except Exception:
        pass


def enabled() -> bool:
    return bool(_load().get("hash"))


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER).hex()


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Use at least 8 characters")
    salt = secrets.token_hex(16)
    _save({"salt": salt, "hash": _hash(password, salt), "sessions": {}})   # old logins end


def disable() -> None:
    _save({})


def locked_for() -> int:
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < LOCK_SECONDS]
    if len(_fails) >= MAX_TRIES:
        return int(LOCK_SECONDS - (now - _fails[0])) + 1
    return 0


def login(password: str) -> str | None:
    """Returns a new session token, or None if the password is wrong."""
    d = _load()
    if not d.get("hash") or locked_for():
        return None
    if not hmac.compare_digest(_hash(password, d["salt"]), d["hash"]):
        _fails.append(time.time())
        return None
    _fails.clear()
    token = secrets.token_urlsafe(32)
    sessions = {k: v for k, v in d.get("sessions", {}).items() if v > time.time()}
    sessions[hashlib.sha256(token.encode()).hexdigest()] = time.time() + SESSION_DAYS * 86400
    d["sessions"] = sessions
    _save(d)
    return token


def valid(token: str | None) -> bool:
    if not token:
        return False
    exp = _load().get("sessions", {}).get(hashlib.sha256(token.encode()).hexdigest())
    return bool(exp and exp > time.time())


def logout(token: str | None) -> None:
    if not token:
        return
    d = _load()
    d.get("sessions", {}).pop(hashlib.sha256(token.encode()).hexdigest(), None)
    _save(d)
