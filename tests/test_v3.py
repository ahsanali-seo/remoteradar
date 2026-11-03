"""International jobs, clean descriptions, auto email finding, recommendations."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from radar import ai, db, enrich, filters, pipeline
from radar.models import RawJob
from radar.scrapers import international as intl
from radar.scrapers.boards import clean_description, parse_detail

NOW = datetime.now(timezone.utc)


# ---------- international feeds ----------
def test_feed_parsers():
    rem = intl.parse_remotive({"jobs": [{"title": "SEO Manager", "company_name": "Acme", "url": "https://r/1",
                                         "candidate_required_location": "Worldwide", "description": "<p>Lead SEO</p>",
                                         "publication_date": NOW.isoformat(), "salary": "$50k", "job_type": "full_time"}]})
    assert rem[0].title == "SEO Manager" and rem[0].extra["region"] == "international"
    assert "Salary: $50k" in rem[0].text and "Lead SEO" in rem[0].text
    rok = intl.parse_remoteok([{"legal": "x"}, {"position": "Technical SEO", "company": "B", "url": "https://ok/2",
                                                "location": "", "epoch": int(NOW.timestamp()), "salary_min": 40000,
                                                "salary_max": 60000, "tags": ["seo"]}])
    assert len(rok) == 1 and rok[0].location == "Worldwide" and "USD 40,000–60,000" in rok[0].text
    wwr = intl.parse_wwr('<rss><channel><item><title>Cobalt: SEO Lead</title><region>Anywhere in the World</region>'
                         '<link>https://wwr/3</link><pubDate>Tue, 27 Oct 2026 10:00:00 +0000</pubDate>'
                         '<description>&lt;p&gt;Own SEO&lt;/p&gt;</description></item></channel></rss>')
    assert wwr[0].company == "Cobalt" and wwr[0].title == "SEO Lead" and "Own SEO" in wwr[0].text
    him = intl.parse_himalayas({"jobs": [{"title": "SEO Specialist", "companyName": "C", "applicationLink": "https://h/4",
                                          "locationRestrictions": ["Pakistan", "India"], "pubDate": int(NOW.timestamp())}]})
    assert him[0].location == "Pakistan, India"
    jc = intl.parse_jobicy({"jobs": [{"jobTitle": "SEO Writer", "companyName": "D", "url": "https://j/5",
                                      "jobGeo": "APAC", "pubDate": "2026-10-28 10:00:00", "jobType": ["full-time"]}]})
    assert jc[0].location == "APAC" and "full-time" in jc[0].text
    wn = intl.parse_workingnomads([{"title": "SEO Analyst", "company_name": "E", "url": "https://w/6",
                                    "tags": "seo,marketing", "location": "Anywhere", "pub_date": NOW.isoformat()}])
    assert wn[0].extra["tags"][:2] == ["seo", "marketing"]


def test_ats_parsers_mark_form_only():
    gh = intl.parse_ats("greenhouse", {"jobs": [{"title": "SEO Lead", "absolute_url": "https://gh/1",
                                                 "location": {"name": "Remote - Worldwide"}, "content": "&lt;p&gt;x&lt;/p&gt;"}]},
                        "Stripe", "stripe")
    assert gh[0].company == "Stripe" and gh[0].extra["ats"] is True and gh[0].location == "Remote - Worldwide"
    lv = intl.parse_ats("lever", [{"text": "SEO Manager", "hostedUrl": "https://lv/1", "workplaceType": "remote",
                                    "categories": {"location": "APAC"}, "descriptionPlain": "Grow organic"}], "Canva", "canva")
    assert lv[0].location == "Remote (APAC)"
    ab = intl.parse_ats("ashby", {"jobs": [{"title": "SEO", "jobUrl": "https://ab/1", "isRemote": True, "location": ""}]},
                        "Kraken", "kraken")
    assert ab[0].location == "Remote"
    assert intl.slug_candidates("NP Digital")[:3] == ["npdigital", "np-digital", "npdigital.com"]


def test_eligibility_and_intl_filter():
    assert filters.eligibility("Worldwide") == "worldwide"
    assert filters.eligibility("Remote - APAC") == "asia"
    assert filters.eligibility("US only") == "restricted"
    assert filters.eligibility("Remote") == "unclear"
    ok = RawJob(source="remotive", title="Senior SEO Manager", location="Anywhere", posted_at=NOW,
                extra={"region": "international", "tags": []})
    assert filters.intl_filter(ok) == (True, "ok")
    us = RawJob(source="remotive", title="SEO Manager", location="USA only", posted_at=NOW, extra={"tags": []})
    assert filters.intl_filter(us)[0] is False
    notseo = RawJob(source="remotive", title="Backend Engineer", location="Worldwide", posted_at=NOW, extra={"tags": []})
    assert filters.intl_filter(notseo)[0] is False


# ---------- clean descriptions ----------
def test_rozee_page_chrome_removed():
    html = """<html><body><nav>Home Jobs</nav><h1>SEO Expert</h1>
    <div class="alert">You have successfully applied for this job.</div>
    <div>Browse similar jobs</div><select><option>1990</option><option>1991</option><option>1992</option></select>
    <div class="jbody"><p>We need an SEO expert for remote work in Islamabad.</p>
    <p>Requirements: 3 years SEO.</p><p>1990 1991 1992 1993 1994 1995</p><p>Show more</p></div></body></html>"""
    job = parse_detail(html, "https://rozee.pk/x", "rozee", {"title": "SEO Expert"}, "Rozee")
    assert "We need an SEO expert" in job.text and "Requirements: 3 years SEO." in job.text
    for junk in ("successfully applied", "Browse similar", "1992", "Show more"):
        assert junk not in job.text
    assert clean_description("Intro\n1 2 3 4 5 6 7 8 9 10 11 12\nEnd") == "Intro\nEnd"


# ---------- emails ----------
def test_email_normalized():
    assert enrich.normalize_email("Info@WWW.MedExamExpert.com.") == "info@medexamexpert.com"
    assert enrich.emails_on_page("<p>info@www.medexamexpert.com</p>", "www.medexamexpert.com") == [
        "info@medexamexpert.com"]


class StubAI:
    """check() result + find_website(), no network."""
    def __init__(self, email="", website="", found_site="https://pixelpeak.pk", score=82, elig="local", match=True):
        self.email, self.website, self.found_site, self.score, self.elig, self.match = email, website, found_site, score, elig, match
        self.searches = 0

    def check(self, text, meta, region="local"):
        return ai.CheckResult(match=self.match, reason="ok", title="SEO Executive", company="Pixel Peak",
                              city="Lahore", contact_email=self.email, company_website=self.website,
                              details={"company": "Pixel Peak", "job_type": "Full-time", "work_mode": "Remote",
                                       "salary": "PKR 150k", "timings": "9-6", "experience": "2 years",
                                       "summary": "Role.\n\nRequirements."},
                              match_score=self.score, match_reason="Strong technical SEO fit", eligibility=self.elig)

    def find_website(self, company, hint=""):
        self.searches += 1
        return self.found_site

    def write(self, profile, template, info, site):
        return "Hi Hiring Team,\n\nPixel Peak needs technical SEO; I grew organic traffic 60% for an ecommerce client.\n\nAhsan Ali"


class StubGmail:
    def __init__(self):
        self.drafts = []

    def create_draft(self, to, subject, body, attachment=None, **k):
        self.drafts.append(to)
        return {"draft_id": f"d{len(self.drafts)}", "thread_id": "t", "message_id_header": "<m>"}


def _job(**kw):
    base = dict(source="linkedin", url="https://www.linkedin.com/jobs/view/1", title="SEO Executive",
                company="Pixel Peak", location="Lahore", text="Remote SEO Executive in Lahore. Hybrid.",
                posted_at=NOW, remote_hint=True)
    base.update(kw)
    return RawJob(**base)


def test_no_email_in_post_found_via_search(monkeypatch):
    monkeypatch.setattr(enrich, "looks_like_company", lambda site, company: True)
    monkeypatch.setattr(enrich, "find_emails", lambda site: ["careers@pixelpeak.pk", "info@pixelpeak.pk"])
    a, g = StubAI(), StubGmail()
    stage = pipeline.Pipeline(a, g, site_fetcher=lambda u: "").process(_job())
    assert stage == "drafted" and a.searches == 1 and g.drafts == ["careers@pixelpeak.pk"]   # pre-made draft
    j = db.list_jobs()[0]
    assert j["email_source"] == "search" and j["company_website"] == "https://pixelpeak.pk"
    assert j["match_score"] == 82 and json.loads(j["details"])["salary"] == "PKR 150k"


def test_post_email_wins_and_is_marked_post(monkeypatch):
    called = []
    monkeypatch.setattr(enrich, "find_emails", lambda site: called.append(site) or [])
    a = StubAI(email="hr@pixelpeak.pk")
    pipeline.Pipeline(a, StubGmail(), site_fetcher=lambda u: "").process(_job(text="Remote SEO Lahore. CV to hr@pixelpeak.pk"))
    j = db.list_jobs()[0]
    assert j["contact_email"] == "hr@pixelpeak.pk" and j["email_source"] == "post" and called == [] and a.searches == 0


def test_wrong_website_rejected(monkeypatch):
    monkeypatch.setattr(enrich, "looks_like_company", lambda site, company: False)
    monkeypatch.setattr(enrich, "find_emails", lambda site: ["x@other.com"])
    stage = pipeline.Pipeline(StubAI(), StubGmail(), site_fetcher=lambda u: "").process(_job())
    assert stage == "no_email" and not db.list_jobs()[0]["contact_email"]


def test_ats_jobs_never_emailed(monkeypatch):
    monkeypatch.setattr(enrich, "find_emails", lambda site: pytest.fail("should not look for email"))
    j = _job(source="ats", url="https://gh/1", location="Remote - Worldwide",
             extra={"region": "international", "ats": True, "tags": []})
    stage = pipeline.Pipeline(StubAI(elig="worldwide"), StubGmail(), site_fetcher=lambda u: "").process(j)
    assert stage == "no_email"
    row = db.list_jobs()[0]
    assert row["region"] == "international" and "own form" in row["stage_reason"]


def test_international_restricted_rejected():
    j = _job(source="remotive", url="https://r/9", location="Remote", extra={"region": "international", "tags": []})
    stage = pipeline.Pipeline(StubAI(elig="restricted"), StubGmail(), site_fetcher=lambda u: "").process(j)
    assert stage == "rejected_ai"


def test_blocked_company_and_30_day_rule(monkeypatch):
    db.block_company("Pixel Peak (Pvt) Ltd")
    assert db.is_blocked_company("pixel peak")
    assert pipeline.Pipeline(StubAI(email="hr@pixelpeak.pk"), StubGmail()).process(_job()) == "filtered"
    db.unblock_company("Pixel Peak")
    db.insert_job({"source": "rozee", "company": "Pixel Peak", "status": "Sent", "sent_at": db.iso()})
    g = StubGmail()
    stage = pipeline.Pipeline(StubAI(email="hr@pixelpeak.pk"), g, site_fetcher=lambda u: "").process(
        _job(url="https://www.linkedin.com/jobs/view/2", title="SEO Specialist"))
    assert stage == "recent" and g.drafts == []


def test_backup_keeps_two_weeks(tmp_home):
    dest = db.backup()
    assert dest.exists() and dest.parent.name == "backups"
    for i in range(20):
        (dest.parent / f"radar-2026-01-{i + 1:02d}.sqlite3").write_bytes(b"x")
    db.backup(keep=14)
    assert len(list(dest.parent.glob("radar-*.sqlite3"))) == 14


def test_report_endpoint():
    from fastapi.testclient import TestClient
    from radar import server
    a = db.insert_job({"source": "linkedin", "title": "SEO", "company": "A", "status": "Sent", "sent_at": db.iso(),
                       "stage": "drafted", "match_score": 90})
    db.update_job(a, replied_at=db.iso())
    db.insert_job({"source": "remotive", "title": "SEO Lead", "company": "B", "status": "New", "stage": "no_email",
                   "match_score": 88, "region": "international"})
    c = TestClient(server.app, headers={"X-Radar": "1"})
    r = c.get("/api/report").json()
    assert r["sent"] == 1 and r["replied"] == 1 and r["reply_rate"] == 100
    assert r["top_matches"][0]["company"] == "B"
    assert c.put(f"/api/jobs/{a}/notes", json={"interview_at": "2026-11-03T15:00", "notes": "HR call"}).status_code == 200
    assert db.get_job(a)["status"] == "Interview"
    assert c.get("/api/report").json()["upcoming"][0]["company"] == "A"
    assert c.post("/api/blocked", json={"name": "Spammy Co"}).json()["ok"]
    assert "spammy" in c.get("/api/blocked").json()


# ---------- Gemini fixes ----------
class Resp:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, json.dumps(data)

    def json(self):
        return self._d


class Http:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def post(self, url, json=None, headers=None):
        self.bodies.append(json)
        return self.replies.pop(0)


def ok(text):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": {}})


def test_gemini_cut_off_reply_is_retried(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    g = ai.Gemini(api_key="k", http=Http([ok('{"body": "Hi Hiring Team,\\n\\nDriving measurable'),
                                          ok('{"body": "Hi Hiring Team, full email"}')]))
    assert g.write("P", "T [Name]", {"title": "SEO"}, "") == "Hi Hiring Team, full email"
    cfg = g.http.bodies[0]["generationConfig"]
    assert cfg["maxOutputTokens"] >= 8192 and cfg["thinkingConfig"]["thinkingBudget"] <= 1024


def test_gemini_drops_thinking_setting_if_unsupported(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    g = ai.Gemini(api_key="k", http=Http([Resp(400, {"error": "thinking_budget is not supported"}),
                                          ok('{"match": false, "reason": "no"}')]))
    assert g.check("SEO remote Lahore", {}).match is False
    assert "thinkingConfig" not in g.http.bodies[1]["generationConfig"]


def test_gemini_website_search(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    g = ai.Gemini(api_key="k", http=Http([ok("https://pixelpeak.pk/"), ok("https://www.linkedin.com/company/x")]))
    assert g.find_website("Pixel Peak", "Lahore") == "https://pixelpeak.pk/"
    assert g.http.bodies[0]["tools"] == [{"google_search": {}}]
    assert g.find_website("Other", "") == ""                     # social pages are not company sites



def test_real_cases_from_first_test_run():
    """Cases from Ahsan's `radar test-intl` output on 30 Oct 2026."""
    from radar.filters import eligibility, intl_filter
    mk = lambda title, loc: RawJob(source="x", title=title, location=loc, posted_at=NOW,
                                   extra={"region": "international", "tags": ["seo"]})
    assert intl_filter(mk("Senior Frontend Engineer", "Worldwide"))[0] is False      # tagged seo, not SEO
    for loc in ("Philippines", "Serbia", "Croatia", "Sydney, New South Wales, Australia",
                "Canada", "Noida, Uttar Pradesh, India", "Bangkok, Bangkok City, Thailand"):
        assert eligibility(loc) == "restricted", loc
    assert eligibility("Remote") == "unclear"
    assert eligibility("Pakistan (Remote)") == "asia"
    assert eligibility("Karachi, Sindh, Pakistan") == "asia"
    assert intl_filter(mk("Off-page SEO Team Lead", "Remote"))[0] is True


def test_empty_board_is_not_a_match():
    from radar.scrapers.international import _has_jobs
    assert _has_jobs("workable", {"name": "x", "jobs": []}) is False
    assert _has_jobs("greenhouse", {"jobs": [{"title": "SEO"}]}) is True
    assert _has_jobs("lever", []) is False
