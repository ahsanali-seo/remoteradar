"""Running on Hugging Face Spaces (RADAR_CLOUD=true).

A free Space has no permanent disk: every restart starts from a clean copy of the code.
So the things that must survive (database, password, profile, CV) live in a PRIVATE
Hugging Face dataset (DATA_REPO), and this module

  1. prepare():   copies them from the dataset into the app folder at start-up,
                  writes token.json from the GMAIL_TOKEN_JSON secret and sets the password;
  2. save():      uploads whatever changed back to the dataset (after runs, every few minutes,
                  and when the Space shuts down);
  3. scheduler:   replaces the Mac's launchd jobs (full runs 4x a day, light check every 30 min,
                  catch-up run after a restart).

Secrets (Space settings → Variables and secrets):
  HF_TOKEN            write token that can read/write DATA_REPO
  DATA_REPO           e.g. ahsanali/remoteradar-data  (a private dataset)
  GEMINI_API_KEY      as on the Mac
  GMAIL_TOKEN_JSON    the whole text of token.json from the Mac
  DASHBOARD_PASSWORD  optional; sets or changes the dashboard password
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import settings

# Files kept in the private dataset, relative to the app folder.
DATA_FILES = ["data/radar.sqlite3", "data/auth.json", "data/ats_cache.json",
              "data/last_full_run.txt", "data/linkedin_last_run.txt"]
PROFILE_GLOBS = ["profile/*.md", "profile/*.pdf", "profile/*.txt",
                 "profile/photo.*"]

FULL_RUN_HOURS = (9, 14, 19, 23)          # Pakistan time, same as the Mac schedule
LIGHT_EVERY_MIN = 30
SAVE_EVERY_MIN = 5
CATCHUP_AFTER_H = 5

_lock = threading.Lock()
_known: dict[str, str] = {}               # path -> hash as last seen in the dataset
_proc: subprocess.Popen | None = None
_status = {"last_save": None, "last_save_error": None, "next_full": None, "restored": False}


def _log(msg: str, level: str = "info") -> None:
    print(f"[cloud] {msg}", flush=True)
    try:
        from . import db
        db.log(None, msg, level)
    except Exception:
        pass


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.environ.get("HF_TOKEN") or None)


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def enabled() -> bool:
    return bool(settings.data_repo and os.environ.get("HF_TOKEN"))


# ------------------------------------------------------------------ start-up
def restore() -> None:
    """Copy the database, password and profile from the private dataset into the app folder."""
    if not enabled():
        _log("DATA_REPO or HF_TOKEN is missing: data will be lost when the Space restarts", "warn")
        return
    from huggingface_hub import snapshot_download
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_download(settings.data_repo, repo_type="dataset", local_dir=tmp,
                              token=os.environ.get("HF_TOKEN"))
            src = Path(tmp)
            n = 0
            for f in src.rglob("*"):
                rel = f.relative_to(src).as_posix()
                if f.is_dir() or rel.startswith(".") or not _tracked(rel):
                    continue
                dest = settings.home / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                _known[rel] = _hash(dest)
                n += 1
        _status["restored"] = True
        print(f"[cloud] Restored {n} files from {settings.data_repo}", flush=True)
    except Exception as e:
        # Never start with an empty database over a good one: stop saving until fixed.
        _status["restored"] = False
        print(f"[cloud] Could not read {settings.data_repo}: {type(e).__name__}: {e}", flush=True)


def _tracked(rel: str) -> bool:
    from fnmatch import fnmatch
    return rel in DATA_FILES or any(fnmatch(rel, g) for g in PROFILE_GLOBS)


def write_gmail_token() -> None:
    """token.json comes from the GMAIL_TOKEN_JSON secret (change the secret to reconnect Gmail)."""
    raw = os.environ.get("GMAIL_TOKEN_JSON", "").strip()
    if not raw:
        print("[cloud] GMAIL_TOKEN_JSON not set: Gmail stays disconnected", flush=True)
        return
    try:
        json.loads(raw)
    except ValueError:
        print("[cloud] GMAIL_TOKEN_JSON is not valid JSON: paste the whole token.json text", flush=True)
        return
    settings.token_path.write_text(raw)
    try:
        settings.token_path.chmod(0o600)
    except Exception:
        pass


def apply_password() -> None:
    """DASHBOARD_PASSWORD sets the password if none is stored, or changes it if it is different."""
    from . import auth
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pw:
        return
    d = auth._load()
    if d.get("hash") and auth.hmac.compare_digest(auth._hash(pw, d["salt"]), d["hash"]):
        return
    try:
        auth.set_password(pw)
        print("[cloud] Dashboard password set from DASHBOARD_PASSWORD", flush=True)
    except ValueError as e:
        print(f"[cloud] DASHBOARD_PASSWORD not used: {e}", flush=True)


def prepare() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    restore()
    write_gmail_token()
    apply_password()


# ------------------------------------------------------------------ saving
def _sqlite_copy(src: Path, dest: Path) -> None:
    a, b = sqlite3.connect(src, timeout=30), sqlite3.connect(dest)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def save(reason: str = "") -> bool:
    """Upload changed files to the dataset. Returns True if something was uploaded."""
    if not enabled():
        return False
    if not _status["restored"]:
        # Reading the dataset failed at start-up; uploading now could replace good data.
        _status["last_save_error"] = "Start-up restore failed; not saving (restart the Space)"
        return False
    from glob import glob

    from huggingface_hub import CommitOperationAdd
    with _lock:
        ops, new_known, tmpdir = [], {}, tempfile.mkdtemp()
        try:
            paths = list(DATA_FILES)
            for g in PROFILE_GLOBS:
                paths += [Path(p).relative_to(settings.home).as_posix()
                          for p in glob(str(settings.home / g))]
            for rel in sorted(set(paths)):
                f = settings.home / rel
                if not f.exists():
                    continue
                if rel.endswith(".sqlite3"):
                    snap = Path(tmpdir) / "radar.sqlite3"
                    _sqlite_copy(f, snap)
                    f = snap
                h = _hash(f)
                if _known.get(rel) == h:
                    continue
                ops.append(CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(f)))
                new_known[rel] = h
            if not ops:
                return False
            _api().create_commit(settings.data_repo, repo_type="dataset", operations=ops,
                                 commit_message=f"RemoteRadar save{': ' + reason if reason else ''}")
            _known.update(new_known)
            _status["last_save"] = datetime.now().isoformat(timespec="seconds")
            _status["last_save_error"] = None
            print(f"[cloud] Saved {len(ops)} file(s) {reason}", flush=True)
            return True
        except Exception as e:
            _status["last_save_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[cloud] Save failed: {_status['last_save_error']}", flush=True)
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def squash() -> None:
    """Keep the dataset small: old versions of the database are dropped once a day."""
    try:
        _api().super_squash_history(settings.data_repo, repo_type="dataset",
                                    commit_message="RemoteRadar data (history squashed)")
        print("[cloud] Dataset history squashed", flush=True)
    except Exception as e:
        print(f"[cloud] Squash skipped: {type(e).__name__}: {e}", flush=True)


# ------------------------------------------------------------------ scheduler
def run_active() -> bool:
    if _proc is not None and _proc.poll() is None:
        return True
    lock = settings.data_dir / "run.lock"
    if not lock.exists():
        return False
    import fcntl
    with open(lock) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def start_run(only: str | None = None, logname: str = "run.log") -> bool:
    global _proc
    if run_active():
        return False
    args = [sys.executable, "-m", "radar.cli", "run"] + (["--only", only] if only else [])
    log = open(settings.data_dir / logname, "a")
    _proc = subprocess.Popen(args, cwd=str(settings.home), stdout=log, stderr=subprocess.STDOUT)
    return True


def _last_full() -> float:
    try:
        return float((settings.data_dir / "last_full_run.txt").read_text())
    except Exception:
        return 0.0


def _next_full(now: datetime) -> datetime:
    for day in (0, 1):
        for h in FULL_RUN_HOURS:
            t = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            t += timedelta(minutes=random.randint(0, 20))      # randomized start, like the Mac
            if t > now:
                return t
    return now + timedelta(hours=5)


def _loop() -> None:
    time.sleep(60)                                    # let the dashboard start first
    now = datetime.now()
    next_full = _next_full(now)
    next_light = now + timedelta(minutes=LIGHT_EVERY_MIN)
    next_save = now + timedelta(minutes=SAVE_EVERY_MIN)
    squashed_on = None
    was_running = False
    if time.time() - _last_full() > CATCHUP_AFTER_H * 3600:
        _log("Catch-up: last full run is over 5 hours old, starting one now")
        start_run()
    while True:
        try:
            now = datetime.now()
            _status["next_full"] = next_full.isoformat(timespec="minutes")
            active = run_active()
            if was_running and not active:
                save("after run")
            was_running = active
            if now >= next_full:
                if start_run():
                    _log("Scheduled full run started")
                    next_full = _next_full(now)
                else:
                    next_full = now + timedelta(minutes=5)          # try again shortly
            elif now >= next_light:
                start_run("intl,gmail,careers", "watch.log")
                next_light = now + timedelta(minutes=LIGHT_EVERY_MIN)
            if now >= next_save:
                if not run_active():
                    save("periodic")
                next_save = now + timedelta(minutes=SAVE_EVERY_MIN)
            if now.hour == 4 and squashed_on != now.date() and not run_active():
                squashed_on = now.date()
                squash()
        except Exception as e:
            print(f"[cloud] Scheduler error: {type(e).__name__}: {e}", flush=True)
        time.sleep(30)


def start_scheduler() -> None:
    threading.Thread(target=_loop, name="radar-scheduler", daemon=True).start()
    print("[cloud] Scheduler on: full runs ~09/14/19/23 PKT, light check every 30 min", flush=True)


def status() -> dict:
    return {"cloud": True, "data_repo": settings.data_repo, "saving": enabled(), **_status}
