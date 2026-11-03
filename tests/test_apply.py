"""Apply by email, finding emails on company sites, send-all, local/international."""
import json

import pytest
from fastapi.testclient import TestClient

from radar import db, enrich, pipeline, server


class R:
    def __init__(self, code, text):
        self.status_code, self.text = code, text


class Site:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url):
        self.calls.append(url)
        for k, v in self.pages.items():
            if url.rstrip("/").endswith(k):
                return R(200, v)
        return R(404, "")


class AllowAll:
    def allowed(self, url):
        return True


def test_emails_on_page_filters_junk():
    html = ('<a href="mailto:careers@pixelpeak.pk?subject=CV">Jobs</a> info@pixelpeak.pk '
            'logo@2x.png noreply@pixelpeak.pk someone@othercompany.com owner@gmail.com')
    assert enrich.emails_on_page(html, "www.pixelpeak.pk") == [
        "careers@pixelpeak.pk", "info@pixelpeak.pk", "owner@gmail.com"]


def test_find_emails_prefers_hiring_address():
    site = Site({"pixelpeak.pk": "<p>Say hello: info@pixelpeak.pk</p>",
                 "/contact": "<p>hello@pixelpeak.pk</p>",
                 "/careers": "<p>Send CVs to jobs@pixelpeak.pk</p>"})
    out = enrich.find_emails("pixelpeak.pk", client=site, robots=AllowAll())
    assert out[0] == "jobs@pixelpeak.pk" and set(out) == {"jobs@pixelpeak.pk", "info@pixelpeak.pk",
                                                          "hello@pixelpeak.pk"}
    assert enrich.find_emails("", client=site, robots=AllowAll()) == []


class FakeAI:
    def write(self, profile, template, info, site):
        return (f"Hi Hiring Team,\n\nYour Shopify brands need technical SEO, and I grew organic traffic 60% "
                f"for an ecommerce client.\n\nApplying for {info['title']} at {info['company']}.\n\nAhsan Ali")


class FakeGmail:
    def __init__(self):
        self.drafts = []

    def create_draft(self, to, subject, body, attachment=None, **k):
        self.drafts.append((to, subject, body, attachment))
        return {"draft_id": "d9", "thread_id": "t9", "message_id_header": "<x@y>"}


def test_draft_for_job_writes_personal_email():
    db.init()
    jid = db.insert_job({"source": "linkedin", "title": "SEO Executive", "company": "Pixel Peak",
                         "text": "Remote SEO Executive for Shopify brands, Lahore.", "stage": "no_email",
                         "status": "New", "contact_email": "jobs@pixelpeak.pk", "email_source": "website"})
    g = FakeGmail()
    out = pipeline.draft_for_job(jid, claude=FakeAI(), gmail=g)
    assert out == {"draft_id": "d9", "existing": False}
    to, subject, body, cv = g.drafts[0]
    assert to == "jobs@pixelpeak.pk" and "SEO Executive" in subject and "Pixel Peak" in body
    assert cv is not None                                      # CV attached
    j = db.get_job(jid)
    assert j["status"] == "Drafted" and j["draft_id"] == "d9"
    assert pipeline.draft_for_job(jid, claude=FakeAI(), gmail=g)["existing"] is True   # no duplicate


def test_draft_for_job_needs_email():
    db.init()
    jid = db.insert_job({"source": "linkedin", "title": "SEO", "status": "New"})
    with pytest.raises(ValueError, match="no email"):
        pipeline.draft_for_job(jid, claude=FakeAI(), gmail=FakeGmail())


def test_job_endpoints_and_regions(monkeypatch):
    db.init()
    c = TestClient(server.app, headers={"X-Radar": "1"})
    a = db.insert_job({"source": "linkedin", "title": "SEO Exec", "company": "A", "status": "New",
                       "text": "Full description here"})
    db.insert_job({"source": "remotive", "title": "SEO Lead", "company": "B", "status": "New",
                   "region": "international"})
    assert [j["company"] for j in c.get("/api/jobs?region=local").json()] == ["A"]
    assert [j["company"] for j in c.get("/api/jobs?region=international").json()] == ["B"]
    assert c.get(f"/api/jobs/{a}").json()["text"] == "Full description here"
    assert c.put(f"/api/jobs/{a}/contact", json={"email": "nope"}).status_code == 400
    assert c.put(f"/api/jobs/{a}/contact", json={"email": "hr@a.pk", "website": "a.pk"}).status_code == 200
    assert db.get_job(a)["contact_email"] == "hr@a.pk" and db.get_job(a)["email_source"] == "you"
    monkeypatch.setattr(enrich, "find_emails", lambda site: ["jobs@a.pk"])
    r = c.post(f"/api/jobs/{a}/find-email", json={}).json()
    assert r["emails"] == ["jobs@a.pk"] and json.loads(db.get_job(a)["found_emails"]) == ["jobs@a.pk"]


def test_send_all_needs_confirm(monkeypatch):
    sent = []

    class MB:
        def send_many(self, ids):
            sent.extend(ids)
            return {"sent": ids, "failed": []}
    monkeypatch.setattr(server, "_mailbox", MB())
    c = TestClient(server.app, headers={"X-Radar": "1"})
    assert c.post("/api/mail/send-all", json={"draft_ids": ["a", "b"]}).status_code == 400
    assert sent == []
    assert c.post("/api/mail/send-all", json={"draft_ids": ["a", "b"], "confirm": True}).json()["sent"] == ["a", "b"]


def test_summary_draft_off_by_default():
    from radar.config import settings
    assert settings.summary_draft is False


def test_template_has_hook_line():
    from pathlib import Path
    t = (Path(server.__file__).parent.parent / "profile" / "template.md").read_text()
    assert "[Hook line]" in t


def test_apply_by_email_blocks_invented_numbers():
    class LyingAI:
        def write(self, *a):
            return "Hi,\n\nI grew traffic 400% for 50 brands.\n\nAhsan"
    db.init()
    jid = db.insert_job({"source": "linkedin", "title": "SEO", "company": "X", "status": "New",
                         "contact_email": "hr@x.pk", "text": "Remote SEO Lahore"})
    g = FakeGmail()
    with pytest.raises(RuntimeError, match="unverified numbers"):
        pipeline.draft_for_job(jid, claude=LyingAI(), gmail=g)
    assert g.drafts == []
