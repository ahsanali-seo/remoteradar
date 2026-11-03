"""Dashboard. On the Mac: http://localhost:8787 (127.0.0.1 only). In the cloud: the Space link,
always behind the password."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import db
from .config import PLATFORMS, settings
from .models import STATUSES

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Ahsan RemoteRadar")
_run_proc: subprocess.Popen | None = None


_OPEN_PATHS = {"/login", "/api/login", "/login-photo", "/healthz"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """With a password set (radar set-password), everything needs a logged-in session."""
    from . import auth
    if settings.cloud and not auth.enabled() and request.url.path != "/healthz":
        # On the internet the dashboard can read and send your Gmail: never open it without a password.
        return HTMLResponse("<h2>RemoteRadar is locked</h2><p>No dashboard password is set. Add the secret "
                            "<b>DASHBOARD_PASSWORD</b> (8+ characters) in the Space settings, then restart "
                            "the Space.</p>", status_code=503)
    if auth.enabled() and request.url.path not in _OPEN_PATHS \
            and not auth.valid(request.cookies.get(auth.COOKIE)):
        if request.url.path.startswith("/api/") or request.url.path.startswith("/screenshots/"):
            return JSONResponse({"detail": "Please log in"}, status_code=401)
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def only_this_dashboard(request: Request, call_next):
    """Every change (POST/PUT/DELETE) must carry X-Radar: 1, which only this dashboard's own
    page sends. Other websites open in your browser therefore can't send mail or change jobs."""
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("x-radar") != "1":
        return JSONResponse({"detail": "Blocked: request did not come from the dashboard"}, status_code=403)
    return await call_next(request)


@app.on_event("startup")
def _startup():
    db.init()
    if settings.cloud:
        from . import cloud
        cloud.start_scheduler()


@app.on_event("shutdown")
def _shutdown():
    if settings.cloud:
        from . import cloud
        cloud.save("shutdown")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/cloud")
def cloud_state():
    if not settings.cloud:
        return {"cloud": False}
    from . import cloud
    return cloud.status()


def today_stats() -> dict:
    from .followups import due_count
    return {
        "new_jobs": db.counter("new_jobs"),
        "duplicates": db.counter("duplicates"),
        "drafts": db.counter("drafts"),
        "draft_cap": settings.daily_draft_cap,
        "followups_due": due_count(),
        "ai_cost": db.ai_cost_today(),
    }


def running() -> bool:
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


def snapshot() -> dict:
    return {"agents": db.agents(), "today": today_stats(), "running": running()}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/state")
def state():
    return snapshot()


@app.get("/api/events/recent")
def events_recent():
    return db.recent_events(0, 80)


@app.get("/api/stream")
async def stream():
    async def gen():
        last_id = 0
        recent = db.recent_events(0, 1)
        last_id = recent[-1]["id"] if recent else 0
        last_snap = ""
        tick = 0
        while True:
            evs = db.recent_events(last_id, 100)
            for e in evs:
                last_id = e["id"]
                yield f"event: log\ndata: {json.dumps(e)}\n\n"
            snap = json.dumps(snapshot(), default=str)
            if snap != last_snap:
                last_snap = snap
                yield f"event: state\ndata: {snap}\n\n"
            tick += 1
            if tick % 15 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs")
def jobs(all: bool = False, region: str | None = None):
    rows = db.list_jobs(include_filtered=all)
    if region:
        rows = [r for r in rows if (r.get("region") or "local") == region]
    for r in rows:
        r.pop("text", None)
    return rows


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    job["found_emails"] = json.loads(job["found_emails"]) if job.get("found_emails") else []
    try:
        job["details"] = json.loads(job["details"]) if job.get("details") else {}
    except ValueError:
        job["details"] = {}
    return job


class ContactIn(BaseModel):
    email: str = ""
    website: str = ""


@app.put("/api/jobs/{job_id}/contact")
def job_contact(job_id: int, body: ContactIn):
    from .mailbox import valid_recipients
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    fields = {}
    if body.email.strip():
        if not valid_recipients(body.email):
            raise HTTPException(400, "That doesn't look like an email address")
        fields.update(contact_email=body.email.strip(), email_source="you")
    if body.website.strip():
        fields["company_website"] = body.website.strip()
    if fields:
        db.update_job(job_id, **fields)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/find-email")
def job_find_email(job_id: int, body: ContactIn):
    from .enrich import find_emails
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    site = body.website.strip() or job.get("company_website") or ""
    if not site:
        raise HTTPException(400, "Add the company's website first, then I can look for an email on it.")
    emails = find_emails(site)
    fields = {"company_website": site, "found_emails": json.dumps(emails)}
    if emails and not job.get("contact_email"):
        fields.update(contact_email=emails[0], email_source="website")
    db.update_job(job_id, **fields)
    db.log(None, f"{job.get('company') or 'Job'}: {len(emails)} email(s) found on {site}")
    return {"emails": emails, "chosen": fields.get("contact_email") or job.get("contact_email") or ""}


@app.post("/api/jobs/{job_id}/draft")
def job_draft(job_id: int):
    from .pipeline import draft_for_job
    try:
        return draft_for_job(job_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


class StatusIn(BaseModel):
    status: str


@app.post("/api/jobs/{job_id}/status")
def set_status(job_id: int, body: StatusIn):
    if body.status not in STATUSES:
        raise HTTPException(400, f"Status must be one of {STATUSES}")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    fields = {"status": body.status}
    if body.status == "Sent" and not job.get("sent_at"):
        fields["sent_at"] = db.iso()
    db.update_job(job_id, **fields)
    db.log(None, f"{job.get('company') or 'Job'}: marked {body.status}")
    return {"ok": True}


@app.get("/api/export.csv")
def export():
    rows = db.list_jobs(include_filtered=False)
    cols = ["id", "created_at", "source", "company", "title", "city", "url", "contact_email",
            "status", "stage", "stage_reason", "sent_at", "followup1_at", "followup2_at"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=remoteradar-jobs.csv"})


@app.post("/api/run")
def run_now():
    global _run_proc
    if running() or (_run_proc and _run_proc.poll() is None):
        raise HTTPException(409, "A run is already in progress")
    log = open(settings.data_dir / "run.log", "a")
    _run_proc = subprocess.Popen([sys.executable, "-m", "radar.cli", "run"], cwd=str(settings.home),
                                 stdout=log, stderr=subprocess.STDOUT)
    db.log(None, "Run started from the dashboard")
    return {"ok": True}


@app.post("/api/platforms/{platform}/toggle")
def toggle(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404, "Unknown platform")
    a = db.get_agent(platform)
    enabled = 0 if a.get("enabled", 1) else 1
    db.set_agent(platform, "idle" if enabled else "paused",
                 "Resumed" if enabled else "Paused by you", enabled=enabled)
    db.log(platform, "Scraping resumed" if enabled else "Scraping paused by you")
    return {"enabled": bool(enabled)}


@app.post("/api/platforms/{platform}/clear")
def clear(platform: str):
    db.clear_block(platform)
    return {"ok": True}


@app.get("/screenshots/{name}")
def screenshot(name: str):
    p = (settings.screenshots_dir / name).resolve()
    if p.parent != settings.screenshots_dir.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


class QuickIn(BaseModel):
    text: str


@app.post("/api/quickadd")
def quickadd(body: QuickIn):
    text = body.text.strip()
    if len(text) < 10:
        raise HTTPException(400, "Paste the full job message or a link")
    from .pipeline import process_quickadd
    threading.Thread(target=process_quickadd, args=(text,), daemon=True).start()
    return {"ok": True}


# ======================= Mail inside the dashboard =======================
_mailbox = None
# Google's Gmail client (httplib2) is not thread-safe. FastAPI runs requests in parallel
# threads, and parallel Gmail calls corrupt memory and crash Python on macOS
# ("malloc: Incorrect checksum for freed object"). So every Gmail call waits its turn.
_gmail_lock = threading.RLock()


def mailbox():
    global _mailbox
    with _gmail_lock:
        return _make_mailbox()


def _make_mailbox():
    global _mailbox
    if _mailbox is None:
        try:
            from .mailbox import Mailbox
            _mailbox = Mailbox()
        except Exception as e:
            raise HTTPException(503, f"Gmail not connected: {e}. Run `radar gmail-auth`.")
    return _mailbox


def _gmail_call(fn, *a, **k):
    try:
        with _gmail_lock:
            return fn(*a, **k)
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "insufficient" in msg.lower() or "403" in msg:
            raise HTTPException(403, "Gmail did not allow this. Run `radar gmail-auth` again and tick every box.")
        raise HTTPException(502, f"Gmail error: {msg[:200]}")


@app.get("/api/mail/folders")
def mail_folders():
    from .mailbox import FOLDERS
    return [{"key": k, "label": v["label"]} for k, v in FOLDERS.items()]


@app.get("/api/mail/counts")
def mail_counts():
    return _gmail_call(mailbox().counts)


@app.get("/api/mail/list/{folder}")
def mail_list(folder: str, page: str | None = None, q: str = "", sender: str = "", unread: bool = False,
              attachment: bool = False, within_days: int = 0, words: str = ""):
    from .mailbox import FOLDERS
    if folder not in FOLDERS:
        raise HTTPException(404, "Unknown folder")
    return _gmail_call(mailbox().list, folder, page_token=page, q=q, sender=sender, unread=unread,
                       attachment=attachment, within_days=within_days, words=words)


@app.get("/api/mail/message/{mid}")
def mail_get(mid: str):
    return _gmail_call(mailbox().get, mid)


@app.get("/api/mail/message/{mid}/attachment/{att_id}")
def mail_attachment(mid: str, att_id: str, name: str = "attachment"):
    data = _gmail_call(mailbox().attachment, mid, att_id)
    safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:120] or "attachment"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


class ActionIn(BaseModel):
    action: str


@app.post("/api/mail/message/{mid}/action")
def mail_action(mid: str, body: ActionIn):
    try:
        _gmail_call(mailbox().action, mid, body.action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/mail/draft/{draft_id}")
def draft_get(draft_id: str):
    return _gmail_call(mailbox().get_draft, draft_id)


class DraftIn(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""


@app.put("/api/mail/draft/{draft_id}")
def draft_save(draft_id: str, body: DraftIn):
    from .mailbox import valid_recipients
    if not valid_recipients(body.to):
        raise HTTPException(400, "The To address doesn't look like an email")
    return _gmail_call(mailbox().update_draft, draft_id, body.to, body.subject, body.body, body.cc)


@app.delete("/api/mail/draft/{draft_id}")
def draft_delete(draft_id: str):
    _gmail_call(mailbox().delete_draft, draft_id)
    return {"ok": True}


class SendIn(BaseModel):
    confirm: bool = False


@app.post("/api/mail/draft/{draft_id}/send")
def draft_send(draft_id: str, body: SendIn):
    if body.confirm is not True:
        raise HTTPException(400, "Sending needs your confirmation")
    return _gmail_call(mailbox().send_draft, draft_id)


class ComposeIn(BaseModel):
    to: str
    subject: str = ""
    body: str
    cc: str = ""
    reply_to_id: str | None = None
    attach_cv: bool = False
    send: bool = False
    confirm: bool = False


@app.post("/api/mail/compose")
def mail_compose(body: ComposeIn):
    from .mailbox import valid_recipients
    if not valid_recipients(body.to):
        raise HTTPException(400, "The To address doesn't look like an email")
    if body.send and body.confirm is not True:
        raise HTTPException(400, "Sending needs your confirmation")
    return _gmail_call(mailbox().compose, body.to, body.subject, body.body, body.cc,
                       body.reply_to_id, body.attach_cv, body.send)


class SendManyIn(BaseModel):
    draft_ids: list[str]
    confirm: bool = False


@app.post("/api/mail/send-all")
def mail_send_all(body: SendManyIn):
    if body.confirm is not True:
        raise HTTPException(400, "Sending needs your confirmation")
    if not body.draft_ids or len(body.draft_ids) > 50:
        raise HTTPException(400, "Choose between 1 and 50 drafts")
    return _gmail_call(mailbox().send_many, body.draft_ids)


# ======================= tracker extras =======================
class JobNotesIn(BaseModel):
    interview_at: str | None = None
    notes: str | None = None


@app.put("/api/jobs/{job_id}/notes")
def job_notes(job_id: int, body: JobNotesIn):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    fields = {}
    if body.interview_at is not None:
        fields["interview_at"] = body.interview_at or None
        if body.interview_at and job.get("status") not in ("Interview",):
            fields["status"] = "Interview"
    if body.notes is not None:
        fields["notes"] = body.notes[:5000]
    db.update_job(job_id, **fields)
    return {"ok": True}


class CompanyIn(BaseModel):
    name: str


@app.get("/api/blocked")
def blocked_list():
    return db.blocked_companies()


@app.post("/api/blocked")
def blocked_add(body: CompanyIn):
    if not body.name.strip():
        raise HTTPException(400, "Company name is empty")
    db.block_company(body.name)
    hidden = 0
    for j in db.jobs_where("status IN ('New','Drafted')"):
        if db._norm_company(j.get("company") or "") == db._norm_company(body.name):
            db.update_job(j["id"], stage="rejected_ai", stage_reason="Company on your do-not-show list")
            hidden += 1
    db.log(None, f"{body.name}: added to your do-not-show list")
    return {"ok": True, "hidden": hidden}


@app.delete("/api/blocked/{name}")
def blocked_remove(name: str):
    db.unblock_company(name)
    return {"ok": True}


@app.get("/api/report")
def report(days: int = 7):
    since = db.iso(db.now() - __import__("datetime").timedelta(days=days))
    found = db.jobs_where("created_at >= ? AND stage NOT IN ('filtered','rejected_ai','duplicate')", (since,))
    sent = db.jobs_where("sent_at IS NOT NULL AND sent_at >= ?", (since,))
    replied = db.jobs_where("replied_at IS NOT NULL AND replied_at >= ?", (since,))
    interviews = db.jobs_where("status = 'Interview'")
    upcoming = sorted([j for j in interviews if j.get("interview_at")], key=lambda j: j["interview_at"])
    by_source: dict = {}
    for j in found:
        b = by_source.setdefault(j["source"], {"found": 0, "sent": 0, "replied": 0})
        b["found"] += 1
    for j in sent:
        by_source.setdefault(j["source"], {"found": 0, "sent": 0, "replied": 0})["sent"] += 1
    for j in replied:
        by_source.setdefault(j["source"], {"found": 0, "sent": 0, "replied": 0})["replied"] += 1
    return {
        "days": days, "found": len(found), "sent": len(sent), "replied": len(replied),
        "interviews": len(interviews),
        "reply_rate": round(100 * len(replied) / len(sent)) if sent else 0,
        "by_source": by_source,
        "upcoming": [{"id": j["id"], "title": j.get("title"), "company": j.get("company"),
                      "interview_at": j.get("interview_at")} for j in upcoming[:8]],
        "top_matches": [{"id": j["id"], "title": j.get("title"), "company": j.get("company"),
                         "match_score": j.get("match_score"), "region": j.get("region") or "local"}
                        for j in sorted([j for j in found if (j.get("match_score") or 0) >= 70
                                         and j.get("status") in ("New", "Drafted")],
                                        key=lambda j: -(j.get("match_score") or 0))[:6]],
    }


@app.get("/api/me")
def me():
    """Details the dashboard needs to write a WhatsApp message (from .env)."""
    return {"name": "Ahsan Ali", "title": "Senior SEO Consultant", "years": 8, "city": "Lahore",
            "portfolio": settings.portfolio, "cv_link": settings.cv_link, "email": settings.gmail_address}


class ChannelIn(BaseModel):
    channel: str


@app.post("/api/jobs/{job_id}/applied")
def job_applied(job_id: int, body: ChannelIn):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    note = (job.get("notes") or "")
    note = (note + f"\nApplied via {body.channel} on {db.today()}").strip()
    db.update_job(job_id, status="Sent", sent_at=job.get("sent_at") or db.iso(), notes=note[:5000])
    db.log(None, f"{job.get('company') or 'Job'}: applied via {body.channel}")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/find-phone")
def job_find_phone(job_id: int):
    from .filters import phones_in
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    nums = phones_in(job.get("text") or "")
    source = "post"
    if not nums:
        from .ai import make_ai
        ai = make_ai()
        ph = ""
        if ai is not None and job.get("company"):
            try:
                ph = ai.find_phone(job["company"], job.get("city") or "Pakistan")
            except Exception:
                ph = ""
        nums, source = ([ph] if ph else []), "search"
    if not nums:
        return {"phone": "", "message": "No WhatsApp number in the post or on Google. Apply by email or on site."}
    db.update_job(job_id, phone=nums[0], phone_source=source)
    return {"phone": nums[0], "source": source}


class PhoneIn(BaseModel):
    phone: str


@app.put("/api/jobs/{job_id}/phone")
def job_set_phone(job_id: int, body: PhoneIn):
    from .filters import phones_in
    nums = phones_in(body.phone)
    if not nums:
        raise HTTPException(400, "Use a Pakistani mobile number like 0300 1234567")
    db.update_job(job_id, phone=nums[0], phone_source="you")
    return {"phone": nums[0]}


# ======================= password =======================
@app.get("/login", response_class=HTMLResponse)
def login_page():
    from . import auth
    if not auth.enabled():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def do_login(body: LoginIn):
    from . import auth
    wait = auth.locked_for()
    if wait:
        raise HTTPException(429, f"Too many wrong tries. Wait {wait // 60 + 1} min.")
    token = auth.login(body.password)
    if not token:
        left = auth.MAX_TRIES - len(auth._fails)
        raise HTTPException(401, "Wrong password" + (f" ({left} tries left)" if left > 0 else ""))
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict",
                 path="/", secure=settings.cloud)
    return r


@app.post("/api/logout")
def do_logout(request: Request):
    from . import auth
    auth.logout(request.cookies.get(auth.COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE, path="/")
    return r


@app.get("/api/auth")
def auth_state():
    from . import auth
    return {"enabled": auth.enabled()}


@app.get("/login-photo")
def login_photo():
    """Your photo for the login page: profile/photo.jpg (or .jpeg / .png / .webp)."""
    for name in ("photo.jpg", "photo.jpeg", "photo.png", "photo.webp", "photo.JPG", "photo.PNG"):
        f = settings.profile_dir / name
        if f.exists() and f.stat().st_size < 15_000_000:
            return FileResponse(f, headers={"Cache-Control": "no-cache"})
    raise HTTPException(404)
