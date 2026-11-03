"""Mail inside the dashboard, with a fake Gmail service."""
import base64
import email
import email.policy
import pathlib
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from radar import db, mailbox
from radar.config import settings


def b64(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).decode()


class Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v() if callable(self.v) else self.v


class FakeGmail:
    """Tiny in-memory Gmail: messages, drafts, labels, threads."""

    def __init__(self):
        self.calls = []
        self.sent = []
        self.msgs = {
            "m1": {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "snippet": "We liked your CV",
                   "payload": {"mimeType": "multipart/mixed", "headers": [
                       {"name": "From", "value": "Sara Khan <sara@nexalabs.pk>"},
                       {"name": "To", "value": settings.gmail_address},
                       {"name": "Subject", "value": "Re: Application for SEO Executive"},
                       {"name": "Date", "value": "Fri, 30 Oct 2026 10:00:00 +0500"},
                       {"name": "Message-ID", "value": "<abc@nexalabs.pk>"}],
                       "parts": [{"mimeType": "text/plain", "body": {"data": b64(b"Can you talk Monday?")}},
                                 {"mimeType": "application/pdf", "filename": "JD.pdf",
                                  "body": {"attachmentId": "att1", "size": 10}}]}},
        }
        raw = EmailMessage()
        raw["To"] = "hr@acme.pk"
        raw["Subject"] = "Application for SEO Executive"
        raw.set_content("Dear Hiring Team, ...")
        raw.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="Ahsan-Ali-SEO-CV.pdf")
        self.store = {"d1": {"id": "d1", "message": {"id": "dm1", "threadId": "t9",
                                                     "labelIds": ["DRAFT", "L_READY"],
                                                     "raw": b64(raw.as_bytes())}}}

    # resource chain
    def users(self):
        return self

    def messages(self):
        return Msgs(self)

    def drafts(self):
        return Drafts(self)

    def labels(self):
        return Labels(self)

    def threads(self):
        return Threads(self)


class Msgs:
    def __init__(self, g):
        self.g = g

    def list(self, **kw):
        self.g.calls.append(("list", kw))
        return Exec({"messages": [{"id": "m1"}], "nextPageToken": None})

    def get(self, userId, id, format="full", metadataHeaders=None):
        if id == "dm1":
            return Exec({"id": "dm1", "threadId": "t9", "labelIds": ["DRAFT", "L_READY"],
                         "payload": {"headers": [{"name": "To", "value": "hr@acme.pk"},
                                                 {"name": "Subject", "value": "Application for SEO Executive"}]}})
        return Exec(self.g.msgs[id])

    def modify(self, userId, id, body):
        self.g.calls.append(("modify", id, body))
        return Exec({})

    def trash(self, userId, id):
        self.g.calls.append(("trash", id))
        return Exec({})

    def attachments(self):
        return self

    def untrash(self, userId, id):
        return Exec({})


class Drafts:
    def __init__(self, g):
        self.g = g

    def list(self, **kw):
        return Exec({"drafts": [{"id": d, "message": {"id": v["message"]["id"]}} for d, v in self.g.store.items()]})

    def get(self, userId, id, format="full"):
        d = self.g.store[id]
        if format == "raw":
            return Exec(d)
        msg = email.message_from_bytes(base64.urlsafe_b64decode(d["message"]["raw"]), policy=email.policy.default)
        parts = []
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and not p.get_filename():
                parts.append({"mimeType": "text/plain", "body": {"data": b64(p.get_content().encode())}})
            elif p.get_filename():
                parts.append({"mimeType": "application/pdf", "filename": p.get_filename(), "body": {"size": 4}})
        return Exec({"id": id, "message": {"id": d["message"]["id"], "threadId": "t9", "labelIds": d["message"]["labelIds"],
                                            "payload": {"headers": [{"name": k, "value": v} for k, v in msg.items()],
                                                        "parts": parts}}})

    def update(self, userId, id, body):
        self.g.store[id]["message"]["raw"] = body["message"]["raw"]
        self.g.calls.append(("update", id, body))
        return Exec({"id": id, "message": {"id": "dm2"}})

    def create(self, userId, body):
        self.g.calls.append(("create", body))
        self.g.store["d2"] = {"id": "d2", "message": {"id": "dm3", "labelIds": ["DRAFT"], **body["message"]}}
        return Exec({"id": "d2", "message": {"id": "dm3"}})

    def send(self, userId, body):
        self.g.sent.append(body["id"])
        return Exec({"id": "sent1", "threadId": "t9"})

    def delete(self, userId, id):
        self.g.store.pop(id, None)
        return Exec({})


class Labels:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return Exec({"labels": [{"id": "L_READY", "name": settings.gmail_label}]})

    def get(self, userId, id):
        return Exec({"messagesUnread": 3, "messagesTotal": 1})


class Threads:
    def __init__(self, g):
        self.g = g

    def get(self, userId, id, format="metadata", metadataHeaders=None):
        return Exec({"messages": [self.g.msgs["m1"]]})


@pytest.fixture
def mb():
    db.init()
    return mailbox.Mailbox(service=FakeGmail())


def test_query_builder():
    q = mailbox.build_query("inbox", sender="nexalabs.pk", unread=True, attachment=True, within_days=7,
                            words="interview")
    assert q == 'in:inbox from:(nexalabs.pk) "interview" is:unread has:attachment newer_than:7d'
    assert mailbox.build_query("alerts").startswith('label:"')


def test_inbox_and_read(mb):
    r = mb.list("inbox", unread=True)
    item = r["items"][0]
    assert item["from"] == "sara@nexalabs.pk" and item["unread"] and item["attachment"]
    m = mb.get("m1")
    assert m["text"] == "Can you talk Monday?" and m["attachments"][0]["filename"] == "JD.pdf"
    assert m["message_id_header"] == "<abc@nexalabs.pk>"


def test_actions(mb):
    mb.action("m1", "read")
    mb.action("m1", "archive")
    mb.action("m1", "trash")
    calls = [c for c in mb.svc.calls if c[0] in ("modify", "trash")]
    assert calls[0][2] == {"addLabelIds": [], "removeLabelIds": ["UNREAD"]}
    assert calls[1][2]["removeLabelIds"] == ["INBOX"] and calls[2] == ("trash", "m1")
    with pytest.raises(ValueError):
        mb.action("m1", "explode")


def test_ready_drafts_edit_keeps_cv_and_send_marks_job(mb):
    jid = db.insert_job({"source": "rozee", "title": "SEO Executive", "company": "Acme", "status": "Drafted",
                         "draft_id": "d1", "contact_email": "hr@acme.pk"})
    ready = mb.list("ready")["items"]
    assert ready[0]["draft_id"] == "d1" and ready[0]["job"]["company"] == "Acme"
    mb.update_draft("d1", "hr@acme.pk", "Application for SEO Executive - Ahsan", "Edited text")
    raw = mb.svc.store["d1"]["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert msg["Subject"].endswith("Ahsan")
    assert any(p.get_filename() == "Ahsan-Ali-SEO-CV.pdf" for p in msg.walk())   # CV kept
    assert "Edited text" in next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain")
    out = mb.send_draft("d1")
    assert mb.svc.sent == ["d1"] and out["job_ids"] == [jid]
    j = db.get_job(jid)
    assert j["status"] == "Sent" and j["sent_at"] and j["thread_id"] == "t9"


def test_reply_threads_and_attach_cv(mb, tmp_path, monkeypatch):
    out = mb.compose("sara@nexalabs.pk", "", "Monday works.", reply_to_id="m1")
    assert out == {"draft_id": "d2", "sent": False} and mb.svc.sent == []      # saved, not sent
    body = [c for c in mb.svc.calls if c[0] == "create"][-1][1]["message"]
    assert body["threadId"] == "t1"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=email.policy.default)
    assert msg["In-Reply-To"] == "<abc@nexalabs.pk>" and msg["Subject"].startswith("Re: ")


def test_job_replies_and_counts(mb):
    db.insert_job({"source": "linkedin", "title": "SEO Executive", "company": "Nexa", "status": "Replied",
                   "thread_id": "t1"})
    r = mb.list("replies")["items"]
    assert r[0]["from"] == "sara@nexalabs.pk" and r[0]["job"]["company"] == "Nexa"
    c = mb.counts()
    assert c["inbox_unread"] == 3 and c["replies"] == 1


def test_server_needs_header_and_confirmation(monkeypatch):
    from radar import server
    fake = mailbox.Mailbox(service=FakeGmail())
    monkeypatch.setattr(server, "_mailbox", fake)
    bare = TestClient(server.app)
    assert bare.post("/api/mail/draft/d1/send", json={"confirm": True}).status_code == 403   # other websites
    c = TestClient(server.app, headers={"X-Radar": "1"})
    assert c.post("/api/mail/draft/d1/send", json={}).status_code == 400                   # no confirm
    assert fake.svc.sent == []
    assert c.post("/api/mail/draft/d1/send", json={"confirm": True}).status_code == 200
    assert fake.svc.sent == ["d1"]
    assert c.post("/api/mail/compose", json={"to": "not-an-email", "body": "x"}).status_code == 400
    assert c.get("/api/mail/list/inbox").json()["items"][0]["id"] == "m1"
    assert c.get("/api/mail/list/nope").status_code == 404


def test_only_mailbox_can_send():
    """Scrapers, pipeline, follow-ups and summary never send. Only mailbox.py does."""
    root = pathlib.Path(mailbox.__file__).parent
    for p in root.rglob("*.py"):
        if p.name == "mailbox.py":
            continue
        src = p.read_text()
        assert "drafts().send" not in src and "messages().send" not in src, p.name


def test_gmail_calls_never_overlap(monkeypatch):
    """Parallel requests must not reach Gmail at the same time (that crashed Python on macOS)."""
    import threading
    import time
    from radar import server
    active, peak = [0], [0]

    def slow():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return True

    ts = [threading.Thread(target=server._gmail_call, args=(slow,)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1
