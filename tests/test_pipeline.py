"""Full funnel with a fake Claude and fake Gmail: no network, no tokens."""
import inspect
import json
from types import SimpleNamespace

from radar import db, followups, gmail as gmail_mod
from radar.ai import Claude, invented_numbers, leftover_placeholders
from radar.models import RawJob
from radar.pipeline import Pipeline, subject_for
from radar.summary import summary_text


class FakeAPI:
    """Stands in for anthropic.Anthropic().messages"""
    def __init__(self, body="Dear Hiring Team,\n\nI saw the SEO Specialist role at Pixel Orbit Digital on LinkedIn Jobs.\n"
                            "Your focus on Shopify SEO for fashion brands stood out.\nI grew organic traffic 60%.\n\nBest regards,\nAhsan Ali",
                 match=True):
        self.body, self.match, self.calls = body, match, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if kw["model"].startswith("claude-haiku"):
            text = json.dumps({"match": self.match, "reason": "remote SEO in Lahore", "title": "SEO Specialist",
                               "company": "Pixel Orbit Digital", "city": "Lahore",
                               "contact_email": "invented@fake.com", "company_website": "", "hiring_name": ""})
        else:
            text = json.dumps({"body": self.body})
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=500, output_tokens=120,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeGmail:
    def __init__(self):
        self.drafts = []
        self.threads = {}

    def create_draft(self, to, subject, body, attachment=None, thread_id=None, in_reply_to=None, label=True):
        self.drafts.append(dict(to=to, subject=subject, body=body, attachment=attachment, thread_id=thread_id))
        return {"draft_id": f"d{len(self.drafts)}", "thread_id": thread_id or f"t{len(self.drafts)}",
                "message_id_header": "<m@x>"}

    def thread_state(self, tid):
        return self.threads.get(tid, {"sent": False, "replied": False, "last_message_id": "<m@x>"})


def job(**kw):
    base = dict(source="linkedin", url="https://www.linkedin.com/jobs/view/1", title="SEO Specialist",
                company="Pixel Orbit Digital", location="Lahore (Remote)",
                text="Remote SEO Specialist at a Lahore agency. Email careers@pixelorbit.pk",
                remote_hint=True, extra={"where": "LinkedIn Jobs"})
    base.update(kw)
    return RawJob(**base)


def make(api=None, g=None):
    api = api or FakeAPI()
    g = g or FakeGmail()
    return Pipeline(Claude(client=api), g, site_fetcher=lambda u: "Shopify SEO for fashion brands"), api, g


def test_full_draft_and_dedupe():
    pipe, api, g = make()
    assert pipe.process(job()) == "drafted"
    d = g.drafts[0]
    # The email the model "found" is ignored: only the address really in the post is used
    assert d["to"] == "careers@pixelorbit.pk"
    assert d["subject"] == "Application for SEO Specialist - Ahsan Ali, Senior SEO Consultant (8 Years)"
    assert d["attachment"].name == "Ahsan-Ali-SEO-CV.pdf"
    # Profile and template are sent as a cached system block
    write_call = [c for c in api.calls if "sonnet" in c["model"]][0]
    assert write_call["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "<job_post>" in write_call["messages"][0]["content"]
    # Same job from another site: no AI call, no draft
    n = len(api.calls)
    assert pipe.process(job(source="indeed", url="https://pk.indeed.com/viewjob?jk=1")) == "duplicate"
    assert len(api.calls) == n and len(g.drafts) == 1
    assert db.counter("drafts") == 1 and db.counter("duplicates") == 1 and db.ai_cost_today() > 0


def test_filtered_jobs_cost_nothing():
    pipe, api, g = make()
    assert pipe.process(job(url="https://x.pk/2", title="Accountant", company="A", text="Remote Lahore")) == "filtered"
    assert api.calls == []


def test_ai_rejects():
    pipe, api, g = make(FakeAPI(match=False))
    assert pipe.process(job()) == "rejected_ai" and g.drafts == []


def test_no_email_goes_to_summary():
    pipe, api, g = make()
    assert pipe.process(job(text="Remote SEO Specialist at a Lahore agency, apply on our site")) == "no_email"
    assert pipe.stats.apply_jobs and g.drafts == []
    subject, body = summary_text(pipe.stats)
    assert "1 new jobs" in subject and "APPLY ON SITE (1)" in body


def test_invented_numbers_block_the_draft():
    pipe, api, g = make(FakeAPI(body="Dear Team,\nI grew traffic 300% for 40 clients.\nAhsan"))
    assert pipe.process(job()) == "error" and g.drafts == []
    assert invented_numbers("grew 60% in 8 years", "60% growth, 8 years") == []
    assert leftover_placeholders("Hi [Name], <short summary from profile.md here>") == \
        ["[Name]", "<short summary from profile.md here>"]


def test_daily_cap(monkeypatch):
    from radar.config import settings
    monkeypatch.setattr(settings, "daily_draft_cap", 1)
    pipe, api, g = make()
    assert pipe.process(job()) == "drafted"
    assert pipe.process(job(url="https://x.pk/9", company="Other Co", title="SEO Executive",
                            text="Remote SEO executive Lahore. Mail jobs@other.pk")) == "capped"
    assert len(g.drafts) == 1


def test_profile_with_todo_blocks_drafts(tmp_home):
    (tmp_home / "profile" / "profile.md").write_text("- Results: TODO\n")
    pipe, api, g = make()
    assert pipe.process(job()) == "error" and g.drafts == []


def test_followups_and_reply_stop():
    pipe, api, g = make()
    pipe.process(job())
    j = db.list_jobs()[0]
    old = db.iso(db.now().replace(microsecond=0) - __import__("datetime").timedelta(days=5))
    db.update_job(j["id"], status="Sent", sent_at=old)
    followups.check_followups(g)
    fu = g.drafts[-1]
    assert fu["thread_id"] == j["thread_id"] and fu["subject"].startswith("Re: Application for")
    assert "Hiring Team" in fu["body"] and db.get_job(j["id"])["status"] == "Follow-up 1"
    # Not due again until day 10
    followups.check_followups(g)
    assert len(g.drafts) == 2
    # A reply stops everything
    g.threads[j["thread_id"]] = {"sent": True, "replied": True, "last_message_id": "<r@x>"}
    followups.check_followups(g)
    assert db.get_job(j["id"])["status"] == "Replied" and len(g.drafts) == 2


def test_auto_detects_sent():
    pipe, api, g = make()
    pipe.process(job())
    j = db.list_jobs()[0]
    g.threads[j["thread_id"]] = {"sent": True, "replied": False, "last_message_id": "<m@x>"}
    followups.check_followups(g)
    assert db.get_job(j["id"])["status"] == "Sent"


def test_followups_file_sections():
    f1, f2 = followups.load_followups("## Follow-up 1 (day 4)\nA [Role]\n\n## Follow-up 2 (day 10)\nB")
    assert f1 == "A [Role]" and f2 == "B"


def test_gmail_module_never_sends():
    src = inspect.getsource(gmail_mod)
    assert ".send(" not in src and "messages().send" not in src and "drafts().send" not in src


def test_subject():
    assert subject_for("Local SEO Manager") == "Application for Local SEO Manager - Ahsan Ali, Senior SEO Consultant (8 Years)"
