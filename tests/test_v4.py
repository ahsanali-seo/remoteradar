"""Low budget, WhatsApp numbers, cleanups, timeouts, catch-up."""
import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from radar import ai, db, filters, pipeline
from radar.models import RawJob

NOW = datetime.now(timezone.utc)


def test_phones():
    t = "Call 0300-1234567 or +92 321 7654321, office 042-35761234, WhatsApp 923001234567"
    assert filters.phones_in(t) == ["923001234567", "923217654321"]
    assert filters.landlines_in(t) == ["924235761234"]
    assert filters.phones_in("Order no 12345678901") == []


def test_salary_and_budget():
    assert filters.monthly_pkr("PKR 60,000 - 80,000") == 80000
    assert filters.monthly_pkr("Rs 1.2 lac") == 120000
    assert filters.monthly_pkr("50k-70k") == 70000
    assert filters.monthly_pkr("PKR 1,800,000 per year") == 150000
    assert filters.monthly_pkr("$40k") == 0
    assert filters.budget_flag({"salary": "PKR 60,000"}, "SEO Executive") == "low"
    assert filters.budget_flag({"salary": "PKR 250,000"}, "SEO Manager") == ""
    assert filters.budget_flag({"min_years": 1}, "SEO Specialist") == "low"
    assert filters.budget_flag({"min_years": 5, "seniority": "senior"}, "Senior SEO") == ""
    assert filters.budget_flag({}, "SEO Intern") == "low"
    assert filters.budget_flag({"min_years": 1}, "SEO Specialist", region="international") == ""


class StubAI:
    def __init__(self, details, email="hr@acme.pk", phone=""):
        self.details, self.email, self.phone, self.phone_searches = details, email, phone, 0

    def check(self, text, meta, region="local"):
        return ai.CheckResult(match=True, reason="ok", title="SEO Executive", company="Acme", city="Lahore",
                              contact_email=self.email, details=self.details, match_score=80)

    def find_website(self, *a):
        return ""

    def find_phone(self, company, city=""):
        self.phone_searches += 1
        return self.phone

    def write(self, *a):
        return "Hi,\n\nI grew organic traffic 60% for an ecommerce client.\n\nAhsan"


class StubGmail:
    def __init__(self):
        self.drafts = []

    def create_draft(self, to, *a, **k):
        self.drafts.append(to)
        return {"draft_id": "d1", "thread_id": "t1"}


def job(text="Remote SEO Executive, Lahore. CV to hr@acme.pk", company="ROZEE.PK"):
    return RawJob(source="rozee", url="https://rozee.pk/1", title="SEO Executive", company=company,
                  location="Lahore", text=text, posted_at=NOW, remote_hint=True)


def test_low_budget_shown_but_not_auto_drafted():
    g = StubGmail()
    a = StubAI({"salary": "PKR 50,000", "company": "Acme Digital"})
    stage = pipeline.Pipeline(a, g, site_fetcher=lambda u: "").process(job())
    assert stage == "low_budget" and g.drafts == []
    j = db.list_jobs()[0]
    assert j["budget_flag"] == "low" and j["match_score"] == 55          # 80 - 25
    assert j["company"] == "Acme"                                         # not "ROZEE.PK"


def test_phone_from_post_and_search():
    a = StubAI({"salary": "PKR 200,000"}, phone="923331112222")
    pipeline.Pipeline(a, StubGmail(), site_fetcher=lambda u: "").process(
        job(text="Remote SEO Executive Lahore. WhatsApp CV: 0301-2345678. hr@acme.pk"))
    j = db.list_jobs()[0]
    assert j["phone"] == "923012345678" and j["phone_source"] == "post" and a.phone_searches == 0
    pipeline.Pipeline(a, StubGmail(), site_fetcher=lambda u: "").process(
        RawJob(source="rozee", url="https://rozee.pk/2", title="SEO Lead", company="Beta", location="Lahore",
               text="Remote SEO Lead in Lahore. Email hr@acme.pk", posted_at=NOW, remote_hint=True))
    j2 = [x for x in db.list_jobs() if x["url"].endswith("/2")][0]
    assert j2["phone"] == "923331112222" and j2["phone_source"] == "search"


def test_old_rows_cleaned_on_start():
    jid = db.insert_job({"source": "rozee", "company": "ROZEE.PK", "status": "New",
                         "contact_email": "info@www.medexamexpert.com",
                         "found_emails": '["info@www.medexamexpert.com"]',
                         "details": json.dumps({"company": "Med Exam Expert Pvt Ltd"})})
    db.init()
    j = db.get_job(jid)
    assert j["contact_email"] == "info@medexamexpert.com"
    assert json.loads(j["found_emails"]) == ["info@medexamexpert.com"]
    assert j["company"] == "Med Exam Expert Pvt Ltd"


def test_gemini_timeout_retried(monkeypatch):
    import httpx
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)

    class Http:
        def __init__(self):
            self.n = 0

        def post(self, *a, **k):
            self.n += 1
            if self.n == 1:
                raise httpx.ReadTimeout("The read operation timed out")

            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"candidates": [{"content": {"parts": [{"text": '{"body": "Hi"}'}]}}]}
            return R()
    g = ai.Gemini(api_key="k", http=Http())
    assert g.write("P", "T", {"title": "SEO"}, "") == "Hi" and g.http.n == 2


def test_applied_via_whatsapp():
    from radar import server
    jid = db.insert_job({"source": "rozee", "company": "Acme", "status": "New", "phone": "923001234567"})
    c = TestClient(server.app, headers={"X-Radar": "1"})
    assert c.post(f"/api/jobs/{jid}/applied", json={"channel": "WhatsApp"}).json()["ok"]
    j = db.get_job(jid)
    assert j["status"] == "Sent" and "WhatsApp" in j["notes"]
    assert c.get("/api/me").json()["years"] == 8
    from radar.followups import due_count
    assert due_count() == 0                 # no email thread, so no email follow-up


def test_catchup_skips_when_recent(tmp_home, monkeypatch, capsys):
    from radar import cli
    from radar.config import settings
    (settings.data_dir / cli.LAST_FULL).write_text(str(time.time()))
    called = []
    monkeypatch.setattr(cli, "cmd_run", lambda a: called.append(1))
    cli.cmd_catchup(type("A", (), {})())
    assert called == [] and "recent" in capsys.readouterr().out


def test_schedule_plists():
    from radar import cli
    run = cli._plist("x.run", ["run"], [(9, 0), (14, 0), (19, 0), (23, 0)], False)
    watch = cli._plist("x.watch", ["run", "--only", "intl,gmail,careers"], None, False, interval=1800)
    catch = cli._plist("x.catchup", ["catchup"], None, False, at_load=True)
    assert run.count("<key>Hour</key>") == 4
    assert "<key>StartInterval</key><integer>1800</integer>" in watch
    assert "<key>RunAtLoad</key><true/>" in catch and "KeepAlive" not in catch
