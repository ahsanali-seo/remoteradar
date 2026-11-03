"""SQLite storage. One file at data/radar.sqlite3; safe to use from several processes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import PLATFORMS, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT, title TEXT, company TEXT, city TEXT, location TEXT,
    posted_at TEXT, text TEXT, author TEXT,
    contact_email TEXT, company_website TEXT, hiring_name TEXT,
    status TEXT NOT NULL DEFAULT 'New',
    stage TEXT,                -- filtered / rejected_ai / no_email / drafted / capped / error
    stage_reason TEXT,
    draft_id TEXT, thread_id TEXT, message_id_header TEXT,
    subject TEXT,
    sent_at TEXT, followup1_at TEXT, followup2_at TEXT, replied_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS blocked_companies (
    name TEXT PRIMARY KEY,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS fingerprints (
    fp TEXT PRIMARY KEY,
    job_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    platform TEXT,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    platform TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',   -- idle scraping thinking done blocked paused error
    detail TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    health TEXT NOT NULL DEFAULT 'OK',     -- OK Warning Blocked
    last_success TEXT,
    screenshot TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, model TEXT, purpose TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, cost REAL
);
CREATE TABLE IF NOT EXISTS counters (
    day TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def today() -> str:
    # Local day on the Mac (Pakistan time on Ahsan's machine)
    return datetime.now().strftime("%Y-%m-%d")


_db_path: Path | None = None


def set_db_path(p: Path) -> None:
    """Tests point the app at a temporary database."""
    global _db_path
    _db_path = p


def path() -> Path:
    return _db_path or settings.db_path


@contextmanager
def connect():
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, ddl in (("region", "TEXT DEFAULT 'local'"), ("email_source", "TEXT"),
                         ("found_emails", "TEXT"), ("details", "TEXT"), ("match_score", "INTEGER"),
                         ("match_reason", "TEXT"), ("eligibility", "TEXT"), ("interview_at", "TEXT"),
                         ("notes", "TEXT"), ("phone", "TEXT"), ("phone_source", "TEXT"),
                         ("budget_flag", "TEXT")):
            if col not in cols:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        # Clean up older rows: 'info@www.site.com' and job boards saved as the company name.
        for jid, em, found in con.execute(
                "SELECT id, contact_email, found_emails FROM jobs WHERE contact_email LIKE '%@www.%' "
                "OR found_emails LIKE '%@www.%'").fetchall():
            con.execute("UPDATE jobs SET contact_email=?, found_emails=? WHERE id=?",
                        ((em or "").replace("@www.", "@") or None, (found or "").replace("@www.", "@") or None, jid))
        from .filters import phones_in
        for jid, text in con.execute("SELECT id, text FROM jobs WHERE (phone IS NULL OR phone='') "
                                     "AND text IS NOT NULL AND text != ''").fetchall():
            nums = phones_in(text)
            if nums:
                con.execute("UPDATE jobs SET phone=?, phone_source='post' WHERE id=?", (nums[0], jid))
        for jid, det in con.execute("SELECT id, details FROM jobs WHERE lower(company) IN "
                                    "('rozee.pk','rozee','indeed','mustakbil','brightspyre')").fetchall():
            try:
                real = (json.loads(det or "{}") or {}).get("company", "")
            except ValueError:
                real = ""
            if real and real.lower() not in ("rozee.pk", "rozee", "indeed", "mustakbil", "brightspyre"):
                con.execute("UPDATE jobs SET company=? WHERE id=?", (real, jid))
        for key in PLATFORMS:
            con.execute(
                "INSERT OR IGNORE INTO agents(platform, updated_at) VALUES (?, ?)", (key, iso())
            )


# ---------- events & agent state (the dashboard reads these) ----------

def log(platform: str | None, message: str, level: str = "info") -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO events(ts, platform, level, message) VALUES (?,?,?,?)",
            (iso(), platform, level, message),
        )
    tag = f"[{platform}] " if platform else ""
    print(f"{tag}{message}", flush=True)


def set_agent(platform: str, status: str, detail: str | None = None, **fields) -> None:
    cols = {"status": status, "detail": detail, "updated_at": iso(), **fields}
    sets = ", ".join(f"{k}=?" for k in cols)
    with connect() as con:
        con.execute(f"UPDATE agents SET {sets} WHERE platform=?", (*cols.values(), platform))


def get_agent(platform: str) -> dict:
    with connect() as con:
        r = con.execute("SELECT * FROM agents WHERE platform=?", (platform,)).fetchone()
    return dict(r) if r else {}


def agents() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM agents").fetchall()
    order = list(PLATFORMS)
    out = [dict(r) for r in rows]
    out.sort(key=lambda a: order.index(a["platform"]) if a["platform"] in order else 99)
    for a in out:
        a["label"] = PLATFORMS.get(a["platform"], a["platform"])
    return out


def platform_available(platform: str) -> tuple[bool, str]:
    a = get_agent(platform)
    if not a:
        return True, ""
    if not a["enabled"]:
        return False, "paused on the dashboard"
    until = parse_iso(a.get("paused_until"))
    if until and until > now():
        return False, f"blocked until {until.astimezone().strftime('%d %b %H:%M')}"
    return True, ""


def block_platform(platform: str, reason: str, screenshot: str | None) -> None:
    until = now() + timedelta(hours=settings.block_pause_hours)
    set_agent(platform, "blocked", reason, paused_until=iso(until), health="Blocked",
              screenshot=screenshot)
    log(platform, f"Stopped: {reason}. Paused for {settings.block_pause_hours} hours. "
                  f"Open the browser with `radar login {platform}` and clear it yourself.", "error")


def clear_block(platform: str) -> None:
    set_agent(platform, "idle", "Cleared by you", paused_until=None, health="OK", screenshot=None)
    log(platform, "Block cleared from the dashboard")


# ---------- counters ----------

def bump(name: str, n: int = 1) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO counters(day, name, value) VALUES (?,?,?) "
            "ON CONFLICT(day, name) DO UPDATE SET value = value + excluded.value",
            (today(), name, n),
        )


def counter(name: str, day: str | None = None) -> int:
    with connect() as con:
        r = con.execute("SELECT value FROM counters WHERE day=? AND name=?",
                        (day or today(), name)).fetchone()
    return r["value"] if r else 0


def record_ai(model: str, purpose: str, usage: dict, cost: float) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO ai_calls(ts, model, purpose, input_tokens, output_tokens, cache_read, "
            "cache_write, cost) VALUES (?,?,?,?,?,?,?,?)",
            (iso(), model, purpose, usage.get("input", 0), usage.get("output", 0),
             usage.get("cache_read", 0), usage.get("cache_write", 0), cost),
        )


def ai_cost_today() -> float:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with connect() as con:
        r = con.execute("SELECT COALESCE(SUM(cost),0) c FROM ai_calls WHERE ts >= ?",
                        (iso(start.astimezone(timezone.utc)),)).fetchone()
    return round(r["c"], 4)


# ---------- jobs ----------

def insert_job(fields: dict) -> int:
    fields = {**fields, "created_at": iso(), "updated_at": iso()}
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    with connect() as con:
        cur = con.execute(f"INSERT INTO jobs({cols}) VALUES ({qs})", tuple(fields.values()))
        return cur.lastrowid


def update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def list_jobs(include_filtered: bool = False) -> list[dict]:
    q = "SELECT * FROM jobs"
    if not include_filtered:
        q += " WHERE COALESCE(stage,'') NOT IN ('filtered','rejected_ai')"
    q += " ORDER BY id DESC"
    with connect() as con:
        return [dict(r) for r in con.execute(q).fetchall()]


def jobs_where(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM jobs WHERE {sql}", params).fetchall()]


def recent_events(after_id: int = 0, limit: int = 200) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def dumps(obj) -> str:
    return json.dumps(obj, default=str)


# ---------- companies you never want to see ----------
def _norm_company(name: str) -> str:
    import re as _re
    n = _re.sub(r"\b(pvt|private|ltd|limited|llc|inc|co|smc|\(pvt\)|pakistan)\b\.?", " ", (name or "").lower())
    return " ".join(_re.sub(r"[^a-z0-9]+", " ", n).split())


def block_company(name: str) -> None:
    key = _norm_company(name)
    if key:
        with connect() as con:
            con.execute("INSERT OR IGNORE INTO blocked_companies(name, created_at) VALUES (?, ?)", (key, iso()))


def unblock_company(name: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM blocked_companies WHERE name=?", (_norm_company(name),))


def blocked_companies() -> list[str]:
    with connect() as con:
        return [r[0] for r in con.execute("SELECT name FROM blocked_companies ORDER BY name").fetchall()]


def is_blocked_company(name: str) -> bool:
    key = _norm_company(name)
    return bool(key) and key in set(blocked_companies())


def recently_applied(company: str, days: int = 30) -> bool:
    """Already sent an application to this company in the last N days?"""
    key = _norm_company(company)
    if not key:
        return False
    since = iso(now() - timedelta(days=days))
    for j in jobs_where("sent_at IS NOT NULL AND sent_at >= ?", (since,)):
        if _norm_company(j.get("company") or "") == key:
            return True
    return False


# ---------- daily backup ----------
def backup(keep: int = 14) -> Path | None:
    """Copy the database once a day to data/backups/, keeping the last two weeks."""
    import sqlite3 as _sq
    src = path()
    if not src.exists():
        return None
    folder = src.parent / "backups"
    folder.mkdir(exist_ok=True)
    dest = folder / f"radar-{today()}.sqlite3"
    if not dest.exists():
        a, b = _sq.connect(src), _sq.connect(dest)
        try:
            a.backup(b)
        finally:
            a.close()
            b.close()
    for old in sorted(folder.glob("radar-*.sqlite3"))[:-keep]:
        old.unlink()
    return dest
