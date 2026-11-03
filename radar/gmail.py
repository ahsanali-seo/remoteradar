"""Gmail: create drafts, label them, read job-alert emails, check threads for replies.
This module deliberately has NO function that sends email."""
from __future__ import annotations

import base64
import mimetypes
import re
from email.message import EmailMessage
from email.utils import parseaddr

from .config import settings
from .models import RawJob

BASE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def scopes() -> list[str]:
    return BASE_SCOPES + ([MODIFY_SCOPE] if settings.gmail_apply_label else [])


def authorize(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if settings.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(settings.token_path), scopes())
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            settings.token_path.write_text(creds.to_json())
            return creds
        except Exception:
            creds = None
    if not interactive:
        if settings.cloud:
            raise RuntimeError("Gmail is not connected. On the Mac run `radar gmail-auth`, then paste the "
                               "new token.json text into the GMAIL_TOKEN_JSON secret of the Space.")
        raise RuntimeError("Gmail is not connected. Run `radar gmail-auth` once.")
    if not settings.credentials_path.exists():
        raise RuntimeError(f"Put your Google OAuth desktop credential at {settings.credentials_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.credentials_path), scopes())
    creds = flow.run_local_server(port=0, prompt="consent")
    settings.token_path.write_text(creds.to_json())
    return creds


class Gmail:
    def __init__(self, service=None):
        if service is None:
            from googleapiclient.discovery import build
            service = build("gmail", "v1", credentials=authorize(), cache_discovery=False)
        self.svc = service
        self._label_id: str | None = None

    # ---- labels ----
    def label_id(self, name: str | None = None) -> str | None:
        name = name or settings.gmail_label
        if not settings.gmail_apply_label:
            return None
        if self._label_id and name == settings.gmail_label:
            return self._label_id
        labels = self.svc.users().labels().list(userId="me").execute().get("labels", [])
        for l in labels:
            if l["name"] == name:
                lid = l["id"]
                break
        else:
            parent = name.split("/")[0]
            if "/" in name and parent not in {l["name"] for l in labels}:
                self.svc.users().labels().create(userId="me", body={"name": parent}).execute()
            lid = self.svc.users().labels().create(
                userId="me", body={"name": name, "labelListVisibility": "labelShow",
                                   "messageListVisibility": "show"}).execute()["id"]
        if name == settings.gmail_label:
            self._label_id = lid
        return lid

    # ---- drafts (never sent) ----
    def create_draft(self, to: str, subject: str, body: str, attachment=None,
                     thread_id: str | None = None, in_reply_to: str | None = None,
                     label: bool = True) -> dict:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = settings.gmail_address
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        if attachment is not None and attachment.exists():
            ctype = mimetypes.guess_type(attachment.name)[0] or "application/pdf"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=attachment.name)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        draft = self.svc.users().drafts().create(userId="me", body={"message": message}).execute()
        m = draft.get("message", {})
        lid = self.label_id() if label else None
        if lid and m.get("id"):
            try:
                self.svc.users().messages().modify(userId="me", id=m["id"],
                                                   body={"addLabelIds": [lid]}).execute()
            except Exception:
                pass  # label is a convenience; the draft itself matters
        mid = self.message_id_header(m.get("id")) if m.get("id") else None
        return {"draft_id": draft.get("id"), "thread_id": m.get("threadId"),
                "message_id": m.get("id"), "message_id_header": mid}

    def message_id_header(self, msg_id: str) -> str | None:
        try:
            m = self.svc.users().messages().get(userId="me", id=msg_id, format="metadata",
                                                metadataHeaders=["Message-ID", "Message-Id"]).execute()
            for h in m.get("payload", {}).get("headers", []):
                if h["name"].lower() == "message-id":
                    return h["value"]
        except Exception:
            pass
        return None

    # ---- threads ----
    def thread(self, thread_id: str) -> dict:
        return self.svc.users().threads().get(userId="me", id=thread_id, format="metadata",
                                              metadataHeaders=["From", "Message-ID", "Subject"]).execute()

    def thread_state(self, thread_id: str) -> dict:
        """{'sent': bool, 'replied': bool, 'last_message_id': str}"""
        t = self.thread(thread_id)
        me = settings.gmail_address.lower()
        sent = replied = False
        last_mid = None
        for m in t.get("messages", []):
            labels = m.get("labelIds", [])
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            frm = parseaddr(headers.get("from", ""))[1].lower()
            if "DRAFT" in labels:
                continue
            if "SENT" in labels or frm == me:
                sent = True
            elif frm and frm != me:
                replied = True
            last_mid = headers.get("message-id", last_mid)
        return {"sent": sent, "replied": replied, "last_message_id": last_mid}

    # ---- job alert emails as a backup source ----
    def alert_jobs(self, days: int = 2) -> list[RawJob]:
        q = f'label:"{settings.alerts_label}" newer_than:{days}d'
        res = self.svc.users().messages().list(userId="me", q=q, maxResults=30).execute()
        jobs = []
        for ref in res.get("messages", []):
            m = self.svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            html, plain = _bodies(m.get("payload", {}))
            frm = next((h["value"] for h in m["payload"].get("headers", []) if h["name"] == "From"), "")
            jobs += parse_alert(html, plain, frm)
        return jobs


def _bodies(payload) -> tuple[str, str]:
    html = plain = ""
    stack = [payload]
    while stack:
        p = stack.pop()
        stack += p.get("parts", []) or []
        data = p.get("body", {}).get("data")
        if not data:
            continue
        txt = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        if p.get("mimeType") == "text/html":
            html += txt
        elif p.get("mimeType") == "text/plain":
            plain += txt
    return html, plain


def parse_alert(html: str, plain: str, sender: str = "") -> list[RawJob]:
    from .scrapers.boards import harvest_links
    site = (re.search(r"@([\w.-]+)", sender) or [None, "email"])[1]
    jobs = []
    if html:
        for h in harvest_links(html, "https://example.com", None):
            jobs.append(RawJob(source="gmail", url=h["url"], title=h["title"], text=h["card"],
                               extra={"where": f"a {site} job alert"}))
    elif plain:
        for block in re.split(r"\n\s*\n", plain):
            from .filters import is_seo
            if is_seo(block):
                url = (re.search(r"https?://\S+", block) or [""])[0]
                jobs.append(RawJob(source="gmail", url=url, text=block,
                                   extra={"where": f"a {site} job alert"}))
    return jobs
