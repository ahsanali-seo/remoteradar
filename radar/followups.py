"""Follow-ups on day 4 and day 10 after you send, as replies in the same Gmail thread."""
from __future__ import annotations

import re
from datetime import timedelta

from . import db
from .config import settings

DEFAULT_FOLLOWUPS = """## Follow-up 1
Hi [Name],

I wanted to follow up on my application for the [Role] position at [Company]. I'm still very interested and happy to share more about my SEO work or do a short call at your convenience.

Best regards,
Ahsan Ali

## Follow-up 2
Hi [Name],

Just a final follow-up on my application for [Role] at [Company]. If the position has been filled, no problem at all, and thank you for your time. I'd be glad to be considered for future SEO roles.

Best regards,
Ahsan Ali
"""

DAY_1, DAY_2 = 4, 10


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^##\s*Follow[- ]?up\s*(\d)\b.*$", text, flags=re.M | re.I)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def load_followups(text: str | None = None) -> tuple[str, str]:
    """profile/followups.md with '## Follow-up 1' and '## Follow-up 2' sections."""
    if text is None:
        p = settings.profile_dir / "followups.md"
        text = p.read_text() if p.exists() else DEFAULT_FOLLOWUPS
    found = _sections(text)
    default = _sections(DEFAULT_FOLLOWUPS)
    return found.get("1", default["1"]), found.get("2", default["2"])


def fill(template: str, job: dict) -> str:
    name = (job.get("hiring_name") or "").split(" ")[0] or "Hiring Team"
    rep = {"[Name]": name, "[Role]": job.get("title") or "SEO", "[Company]": job.get("company") or "your company"}
    for k, v in rep.items():
        template = template.replace(k, v)
    return template


def due(job: dict, now=None) -> int | None:
    """Which follow-up (1 or 2) is due now, if any."""
    now = now or db.now()
    sent = db.parse_iso(job.get("sent_at"))
    if not sent or job.get("status") in ("Replied", "Interview", "Rejected"):
        return None
    if not job.get("followup1_at") and now - sent >= timedelta(days=DAY_1):
        return 1
    if job.get("followup1_at") and not job.get("followup2_at") and now - sent >= timedelta(days=DAY_2):
        return 2
    return None


def due_count() -> int:
    jobs = db.jobs_where("status IN ('Sent','Follow-up 1') AND thread_id IS NOT NULL")
    return sum(1 for j in jobs if due(j))


def check_followups(gmail) -> None:
    # 1. Notice drafts you already sent from Gmail (so you don't have to click "Sent")
    for j in db.jobs_where("status='Drafted' AND thread_id IS NOT NULL"):
        try:
            st = gmail.thread_state(j["thread_id"])
        except Exception:
            continue
        if st["sent"]:
            db.update_job(j["id"], status="Sent", sent_at=db.iso())
            db.log("gmail", f"Noticed you sent the email to {j['company']}")

    # 2. Replies stop follow-ups; due follow-ups become reply drafts
    f1, f2 = load_followups()
    for j in db.jobs_where("status IN ('Sent','Follow-up 1','Follow-up 2') AND thread_id IS NOT NULL"):
        try:
            st = gmail.thread_state(j["thread_id"])
        except Exception:
            continue
        if st["replied"]:
            db.update_job(j["id"], status="Replied", replied_at=db.iso())
            notify("RemoteRadar: reply received", f"{j.get('company') or 'A company'} replied about {j.get('title') or 'your application'}")
            db.log("gmail", f"Reply received from {j['company']}. Follow-ups stopped.")
            continue
        n = due(j)
        if not n:
            continue
        body = fill(f1 if n == 1 else f2, j)
        subj = j.get("subject") or ""
        subject = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        gmail.create_draft(j["contact_email"], subject, body, thread_id=j["thread_id"],
                           in_reply_to=st.get("last_message_id") or j.get("message_id_header"))
        field = "followup1_at" if n == 1 else "followup2_at"
        db.update_job(j["id"], **{field: db.iso(), "status": f"Follow-up {n}"})
        db.log("gmail", f"Follow-up {n} draft created for {j['company']}")


def notify(title: str, message: str) -> None:
    """Mac notification with a sound. Silently does nothing on other systems."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return
    esc = lambda t: t.replace("\\", "").replace('"', "'")[:180]  # noqa: E731
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
                       timeout=5, check=False)
    except Exception:
        pass
