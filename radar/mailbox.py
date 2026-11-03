"""Mail inside the dashboard: inbox, folders, advanced search, reading, editing drafts,
replying, and sending.

Sending rule: the scraper, pipeline, follow-ups and summary NEVER send. Only the two
functions below (send_draft, compose with send=True) send, and the server calls them only
when you press Send in the dashboard and confirm. A sent application marks its job "Sent",
so follow-up drafts start on day 4 and day 10.
"""
from __future__ import annotations

import base64
import email
import email.policy
import mimetypes
import re
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from . import db
from .config import settings

FOLDERS = {
    "inbox": {"label": "Inbox", "q": "in:inbox"},
    "replies": {"label": "Job replies", "q": None},          # threads of tracked jobs
    "ready": {"label": "Ready to send", "q": None},          # drafts with RemoteRadar/Ready
    "drafts": {"label": "All drafts", "q": None},
    "sent": {"label": "Sent", "q": "in:sent"},
    "starred": {"label": "Starred", "q": "is:starred"},
    "alerts": {"label": "Job alerts", "q": None},            # label from settings
    "important": {"label": "Important", "q": "is:important"},
    "spam": {"label": "Spam", "q": "in:spam"},
    "trash": {"label": "Trash", "q": "in:trash"},
}


def build_query(folder: str = "inbox", q: str = "", sender: str = "", unread: bool = False,
                attachment: bool = False, within_days: int = 0, words: str = "") -> str:
    """Advanced search → Gmail search syntax."""
    base = FOLDERS.get(folder, FOLDERS["inbox"])["q"] or ""
    if folder == "alerts":
        base = f'label:"{settings.alerts_label}"'
    parts = [base] if base else []
    if sender:
        parts.append(f"from:({sender.strip()})")
    if words:
        parts.append(f'"{words.strip()}"')
    if unread:
        parts.append("is:unread")
    if attachment:
        parts.append("has:attachment")
    if within_days:
        parts.append(f"newer_than:{int(within_days)}d")
    if q:
        parts.append(q.strip())
    return " ".join(parts)


def _headers(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _date_iso(value: str) -> str:
    try:
        return parsedate_to_datetime(value).isoformat()
    except Exception:
        return ""


def _walk(payload: dict):
    stack = [payload]
    while stack:
        p = stack.pop()
        yield p
        stack += list(reversed(p.get("parts", []) or []))


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")


def summary(m: dict) -> dict:
    h = _headers(m.get("payload", {}))
    labels = m.get("labelIds", [])
    has_att = any(p.get("filename") for p in _walk(m.get("payload", {})))
    name, addr = parseaddr(h.get("from", ""))
    return {
        "id": m["id"], "thread_id": m.get("threadId"),
        "from_name": name or addr, "from": addr, "to": h.get("to", ""),
        "subject": h.get("subject", "(no subject)"), "snippet": m.get("snippet", ""),
        "date": _date_iso(h.get("date", "")),
        "unread": "UNREAD" in labels, "starred": "STARRED" in labels,
        "draft": "DRAFT" in labels, "attachment": has_att,
    }


class Mailbox:
    def __init__(self, service=None):
        if service is None:
            from .gmail import Gmail
            service = Gmail().svc
        self.svc = service
        self.users = service.users()

    # ---------------- lists ----------------
    def _get_meta(self, mid: str) -> dict:
        return self.users.messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()

    def _get_meta_many(self, ids: list[str]) -> dict[str, dict]:
        """One batched HTTP call for many messages instead of one call each (much faster)."""
        if not ids:
            return {}
        make = getattr(self.svc, "new_batch_http_request", None)
        if make is None:                       # test fakes
            return {i: self._get_meta(i) for i in ids}
        out: dict[str, dict] = {}

        def cb(req_id, resp, exc):
            if exc is None and resp:
                out[req_id] = resp

        for start in range(0, len(ids), 50):
            batch = make(callback=cb)
            for i in ids[start:start + 50]:
                batch.add(self.users.messages().get(userId="me", id=i, format="metadata",
                                                    metadataHeaders=["From", "To", "Subject", "Date"]),
                          request_id=i)
            batch.execute()
        missing = [i for i in ids if i not in out]
        for i in missing[:10]:                 # retry the few that failed, one by one
            try:
                out[i] = self._get_meta(i)
            except Exception:
                pass
        return out

    def list(self, folder: str = "inbox", page_token: str | None = None, limit: int = 25,
             **search) -> dict:
        if folder in ("drafts", "ready"):
            return self.list_drafts(ready_only=folder == "ready", limit=limit, page_token=page_token)
        if folder == "replies":
            return self.list_job_replies(limit=limit)
        q = build_query(folder, **search)
        kw = {"userId": "me", "q": q, "maxResults": limit}
        if folder in ("spam", "trash"):
            kw["includeSpamTrash"] = True
        if page_token:
            kw["pageToken"] = page_token
        res = self.users.messages().list(**kw).execute()
        ids = [r["id"] for r in res.get("messages", [])]
        metas = self._get_meta_many(ids)
        items = [summary(metas[i]) for i in ids if i in metas]
        self._attach_jobs(items)
        return {"items": items, "next": res.get("nextPageToken"), "query": q}

    def list_drafts(self, ready_only: bool = False, limit: int = 30, page_token=None) -> dict:
        kw = {"userId": "me", "maxResults": limit}
        if page_token:
            kw["pageToken"] = page_token
        res = self.users.drafts().list(**kw).execute()
        ready_id = self._ready_label_id()
        items = []
        drafts = res.get("drafts", [])
        metas = self._get_meta_many([d["message"]["id"] for d in drafts])
        for d in drafts:
            m = metas.get(d["message"]["id"])
            if not m:
                continue
            if ready_only and ready_id and ready_id not in m.get("labelIds", []):
                continue
            s = summary(m)
            s["draft_id"] = d["id"]
            items.append(s)
        self._attach_jobs(items)
        if ready_only and not ready_id:      # labels off: show drafts that belong to jobs
            items = [i for i in items if i.get("job")]
        return {"items": items, "next": res.get("nextPageToken")}

    def list_job_replies(self, limit: int = 30) -> dict:
        """Latest message from someone else in the thread of each tracked job."""
        me = settings.gmail_address.lower()
        rows = db.jobs_where("thread_id IS NOT NULL AND thread_id != '' ORDER BY updated_at DESC LIMIT ?",
                             (limit,))
        items = []
        for job in rows:
            try:
                t = self.users.threads().get(userId="me", id=job["thread_id"], format="metadata",
                                             metadataHeaders=["From", "To", "Subject", "Date"]).execute()
            except Exception:
                continue
            theirs = [m for m in t.get("messages", [])
                      if parseaddr(_headers(m.get("payload", {})).get("from", ""))[1].lower() != me
                      and "DRAFT" not in m.get("labelIds", [])]
            if theirs:
                s = summary(theirs[-1])
                s["job"] = _job_brief(job)
                items.append(s)
        items.sort(key=lambda i: i["date"], reverse=True)
        return {"items": items, "next": None}

    def _ready_label_id(self) -> str | None:
        try:
            for l in self.users.labels().list(userId="me").execute().get("labels", []):
                if l["name"] == settings.gmail_label:
                    return l["id"]
        except Exception:
            pass
        return None

    def _attach_jobs(self, items: list[dict]) -> None:
        tids = {i["thread_id"] for i in items if i.get("thread_id")}
        if not tids:
            return
        q = ",".join("?" * len(tids))
        jobs = {j["thread_id"]: j for j in db.jobs_where(f"thread_id IN ({q})", tuple(tids))}
        drafts = {}
        dids = [i["draft_id"] for i in items if i.get("draft_id")]
        if dids:
            q2 = ",".join("?" * len(dids))
            drafts = {j["draft_id"]: j for j in db.jobs_where(f"draft_id IN ({q2})", tuple(dids))}
        for i in items:
            j = drafts.get(i.get("draft_id")) or jobs.get(i.get("thread_id"))
            if j:
                i["job"] = _job_brief(j)

    def counts(self) -> dict:
        out = {"inbox_unread": 0, "ready": 0, "replies": 0}
        try:
            out["inbox_unread"] = self.users.labels().get(userId="me", id="INBOX").execute().get(
                "messagesUnread", 0)
        except Exception:
            pass
        rid = self._ready_label_id()
        if rid:
            try:
                out["ready"] = self.users.labels().get(userId="me", id=rid).execute().get(
                    "messagesTotal", 0)
            except Exception:
                pass
        out["replies"] = len(db.jobs_where("status IN ('Replied','Interview')"))
        return out

    # ---------------- read ----------------
    def get(self, mid: str) -> dict:
        m = self.users.messages().get(userId="me", id=mid, format="full").execute()
        s = summary(m)
        h = _headers(m.get("payload", {}))
        html = text = ""
        atts = []
        for p in _walk(m.get("payload", {})):
            body = p.get("body", {})
            if p.get("filename"):
                atts.append({"id": body.get("attachmentId"), "filename": p["filename"],
                             "mime": p.get("mimeType"), "size": body.get("size", 0)})
            elif body.get("data"):
                if p.get("mimeType") == "text/html" and not html:
                    html = _decode(body["data"])
                elif p.get("mimeType") == "text/plain" and not text:
                    text = _decode(body["data"])
        s.update({"cc": h.get("cc", ""), "reply_to": h.get("reply-to", ""),
                  "message_id_header": h.get("message-id", ""), "references": h.get("references", ""),
                  "html": html, "text": text, "attachments": atts, "labels": m.get("labelIds", [])})
        self._attach_jobs([s])
        return s

    def attachment(self, mid: str, att_id: str) -> bytes:
        a = self.users.messages().attachments().get(userId="me", messageId=mid, id=att_id).execute()
        return base64.urlsafe_b64decode(a["data"] + "==")

    # ---------------- actions (need the gmail.modify permission) ----------------
    def modify(self, mid: str, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        self.users.messages().modify(userId="me", id=mid,
                                     body={"addLabelIds": add or [], "removeLabelIds": remove or []}).execute()

    def action(self, mid: str, act: str) -> None:
        table = {
            "read": ([], ["UNREAD"]), "unread": (["UNREAD"], []),
            "star": (["STARRED"], []), "unstar": ([], ["STARRED"]),
            "archive": ([], ["INBOX"]), "inbox": (["INBOX"], []),
            "spam": (["SPAM"], ["INBOX"]), "notspam": (["INBOX"], ["SPAM"]),
        }
        if act == "trash":
            self.users.messages().trash(userId="me", id=mid).execute()
        elif act == "untrash":
            self.users.messages().untrash(userId="me", id=mid).execute()
        elif act in table:
            self.modify(mid, *table[act])
        else:
            raise ValueError(f"Unknown action {act}")

    # ---------------- drafts ----------------
    def get_draft(self, draft_id: str) -> dict:
        d = self.users.drafts().get(userId="me", id=draft_id, format="full").execute()
        m = d["message"]
        s = summary(m)
        text = ""
        atts = []
        for p in _walk(m.get("payload", {})):
            if p.get("filename"):
                atts.append({"filename": p["filename"], "size": p.get("body", {}).get("size", 0)})
            elif p.get("mimeType") == "text/plain" and p.get("body", {}).get("data") and not text:
                text = _decode(p["body"]["data"])
        h = _headers(m.get("payload", {}))
        s.update({"draft_id": draft_id, "to": h.get("to", ""), "cc": h.get("cc", ""),
                  "body": text, "attachments": atts})
        self._attach_jobs([s])
        return s

    def update_draft(self, draft_id: str, to: str, subject: str, body: str, cc: str = "") -> dict:
        """Edit To / Subject / text while keeping attachments, thread and reply headers."""
        d = self.users.drafts().get(userId="me", id=draft_id, format="raw").execute()
        msg = email.message_from_bytes(base64.urlsafe_b64decode(d["message"]["raw"] + "=="),
                                       policy=email.policy.default)
        for k, v in (("To", to), ("Subject", subject), ("Cc", cc)):
            if k in msg:
                del msg[k]
            if v:
                msg[k] = v
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                part.set_content(body)
                break
        else:
            if not msg.is_multipart():
                msg.set_content(body)
        message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if d["message"].get("threadId"):
            message["threadId"] = d["message"]["threadId"]
        out = self.users.drafts().update(userId="me", id=draft_id, body={"id": draft_id, "message": message}).execute()
        # Keep the Ready label on the new draft message
        rid = self._ready_label_id()
        new_mid = out.get("message", {}).get("id")
        if rid and new_mid and rid in d["message"].get("labelIds", []):
            try:
                self.modify(new_mid, add=[rid])
            except Exception:
                pass
        return {"draft_id": out.get("id"), "message_id": new_mid}

    def delete_draft(self, draft_id: str) -> None:
        self.users.drafts().delete(userId="me", id=draft_id).execute()
        for job in db.jobs_where("draft_id = ?", (draft_id,)):
            db.update_job(job["id"], draft_id=None, status="New", stage_reason="Draft deleted by you")

    def send_draft(self, draft_id: str) -> dict:
        """Sends ONE draft. Only called after you press Send and confirm."""
        jobs = db.jobs_where("draft_id = ?", (draft_id,))
        sent = self.users.drafts().send(userId="me", body={"id": draft_id}).execute()
        tid = sent.get("threadId")
        for job in jobs:
            db.update_job(job["id"], status="Sent", sent_at=db.iso(), thread_id=tid or job.get("thread_id"),
                          stage_reason="Sent from the dashboard")
            db.log("gmail", f"Sent application: {job.get('title') or 'SEO role'} at {job.get('company') or '?'}")
        if not jobs:
            db.log("gmail", "Email sent from the dashboard")
        return {"message_id": sent.get("id"), "thread_id": tid, "job_ids": [j["id"] for j in jobs]}

    def send_many(self, draft_ids: list[str], gap: float = 3.0) -> dict:
        """Send several drafts you selected, a few seconds apart (looks natural to spam filters)."""
        import time
        sent, failed = [], []
        for n, did in enumerate(draft_ids):
            try:
                self.send_draft(did)
                sent.append(did)
            except Exception as e:
                failed.append({"draft_id": did, "error": str(e)[:200]})
            if n < len(draft_ids) - 1 and gap:
                time.sleep(gap)
        return {"sent": sent, "failed": failed}

    # ---------------- compose / reply ----------------
    def compose(self, to: str, subject: str, body: str, cc: str = "", reply_to_id: str | None = None,
                attach_cv: bool = False, send: bool = False) -> dict:
        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["From"] = settings.gmail_address
        thread_id = None
        if reply_to_id:
            orig = self.users.messages().get(userId="me", id=reply_to_id, format="metadata",
                                             metadataHeaders=["Message-ID", "References", "Subject"]).execute()
            oh = _headers(orig.get("payload", {}))
            mid = oh.get("message-id", "")
            if mid:
                msg["In-Reply-To"] = mid
                msg["References"] = (oh.get("references", "") + " " + mid).strip()
            thread_id = orig.get("threadId")
            if not subject:
                s0 = oh.get("subject", "")
                subject = s0 if s0.lower().startswith("re:") else f"Re: {s0}"
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body)
        if attach_cv:
            cv = settings.cv_path
            if cv and Path(cv).exists():
                ctype = mimetypes.guess_type(str(cv))[0] or "application/pdf"
                mt, st = ctype.split("/", 1)
                msg.add_attachment(Path(cv).read_bytes(), maintype=mt, subtype=st, filename=Path(cv).name)
        message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            message["threadId"] = thread_id
        draft = self.users.drafts().create(userId="me", body={"message": message}).execute()
        if not send:
            return {"draft_id": draft["id"], "sent": False}
        out = self.send_draft(draft["id"])
        out["sent"] = True
        return out


def _job_brief(j: dict) -> dict:
    return {"id": j["id"], "title": j.get("title") or "SEO role", "company": j.get("company") or "",
            "status": j.get("status"), "url": j.get("url") or ""}


def valid_recipients(value: str) -> bool:
    addrs = [a for _, a in getaddresses([value or ""])]
    return bool(addrs) and all(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a) for a in addrs)
