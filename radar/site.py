"""Read-only dashboard for GitHub Pages (or any static host).

The page is public, but everything on it is encrypted. The key is the same PBKDF2 hash the
dashboard password already uses (data/auth.json), so the page opens with the SAME password as
the Mac dashboard. Without the password the page shows only a login box.

    python -m radar.cli site --out site
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path

from . import auth, db
from .config import settings

TEMPLATE = Path(__file__).parent / "static" / "site.html"
TEXT_LIMIT = 6000
JOB_FIELDS = ["id", "created_at", "source", "url", "title", "company", "city", "location", "posted_at",
              "contact_email", "email_source", "company_website", "hiring_name", "status", "stage",
              "stage_reason", "draft_id", "subject", "sent_at", "followup1_at", "followup2_at", "replied_at",
              "region", "match_score", "match_reason", "eligibility", "interview_at", "notes", "phone",
              "phone_source", "budget_flag"]


def _actions_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/actions/workflows/radar.yml" if repo else ""


def _draft_messages(jobs: list[dict]) -> dict[str, str]:
    """Gmail opens a draft by its message id, so look those up (only for drafts not sent yet)."""
    want = [j["draft_id"] for j in jobs if j.get("draft_id") and not j.get("sent_at")][:80]
    if not want:
        return {}
    try:
        from .gmail import Gmail
        svc = Gmail().svc
    except Exception:
        return {}
    out = {}
    for did in want:
        try:
            d = svc.users().drafts().get(userId="me", id=did, format="minimal").execute()
            if d.get("message", {}).get("id"):
                out[did] = d["message"]["id"]
        except Exception:
            pass          # draft deleted or already sent
    return out


def payload() -> dict:
    blocked = {c.lower() for c in db.blocked_companies()}
    jobs = []
    for r in db.list_jobs():
        if (r.get("company") or "").strip().lower() in blocked:
            continue
        j = {k: r.get(k) for k in JOB_FIELDS}
        try:
            j["details"] = json.loads(r["details"]) if r.get("details") else {}
        except ValueError:
            j["details"] = {}
        j["text"] = (r.get("text") or "")[:TEXT_LIMIT]
        jobs.append(j)
    msgs = _draft_messages(jobs)
    for j in jobs:
        j["draft_msg"] = msgs.get(j.get("draft_id") or "")

    since = db.iso(db.now() - timedelta(days=7))
    found = db.jobs_where("created_at >= ? AND stage NOT IN ('filtered','rejected_ai','duplicate')", (since,))
    sent = db.jobs_where("sent_at IS NOT NULL AND sent_at >= ?", (since,))
    replied = db.jobs_where("replied_at IS NOT NULL AND replied_at >= ?", (since,))
    from .followups import due_count
    return {
        "generated": db.iso(),
        "me": {"name": "Ahsan Ali", "title": "Senior SEO Consultant", "years": 8, "city": "Lahore",
               "portfolio": settings.portfolio, "cv_link": settings.cv_link, "email": settings.gmail_address},
        "gmail_label": settings.gmail_label if settings.gmail_apply_label else "",
        "actions_url": _actions_url(),
        "today": {"new_jobs": db.counter("new_jobs"), "duplicates": db.counter("duplicates"),
                  "drafts": db.counter("drafts"), "draft_cap": settings.daily_draft_cap,
                  "followups_due": due_count()},
        "week": {"found": len(found), "sent": len(sent), "replied": len(replied),
                 "reply_rate": round(100 * len(replied) / len(sent)) if sent else 0},
        "agents": db.agents(),
        "events": db.recent_events(0, 60),
        "jobs": jobs,
    }


def encrypt(data: dict) -> dict | None:
    """AES-256-GCM with the dashboard password's PBKDF2 hash as the key. None if no password is set."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    a = auth._load()
    if not a.get("hash") or not a.get("salt"):
        return None
    key = bytes.fromhex(a["hash"])
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(data, default=str).encode(), None)
    return {"v": 1, "salt": a["salt"], "iter": auth._ITER,
            "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}


def build(out: Path) -> Path:
    db.init()
    out.mkdir(parents=True, exist_ok=True)
    box = encrypt(payload())
    html = TEMPLATE.read_text()
    blob = json.dumps(box) if box else "null"      # null: the page says a password must be set first
    html = html.replace("/*__RADAR_DATA__*/null", blob.replace("</", "<\\/"))
    page = out / "index.html"
    page.write_text(html)
    (out / ".nojekyll").write_text("")
    return page
