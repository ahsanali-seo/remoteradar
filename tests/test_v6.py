"""v6: new job feeds, Google search through Gemini, cover letters for apply-on-site jobs."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from radar import db
from radar.ai import Claude
from radar.models import RawJob
from radar.pipeline import Pipeline, build_scrapers, cover_letter_for_job
from radar.scrapers import international as intl
from radar.scrapers.google import GoogleJobsScraper, grounded, parse_answer

RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title><![CDATA[Senior SEO Manager (Remote) at Acme Growth]]></title>
<link>https://www.realworkfromanywhere.com/jobs/senior-seo-manager-acme</link>
<description>&lt;p&gt;Lead &lt;b&gt;SEO&lt;/b&gt; for our SaaS. Work from anywhere.&lt;/p&gt;</description>
<pubDate>Wed, 11 Nov 2026 08:13:20 GMT</pubDate></item>
<item><title>Backend Engineer at Other Co</title><link>https://x.example/2</link><description>Go</description></item>
</channel></rss>"""


def test_rss_feed_parsers():
    jobs = intl.parse_rwfa(RSS)
    assert [j.title for j in jobs] == ["Senior SEO Manager (Remote)", "Backend Engineer"]
    j = jobs[0]
    assert j.company == "Acme Growth" and j.source == "rwfa" and "Lead SEO for our SaaS" in j.text
    assert j.extra["region"] == "international" and j.posted_at.day == 11
    assert intl.parse_remotefirstjobs(RSS)[0].location == "Remote"
    assert intl.parse_wwr("not xml") == [] and intl.parse_rwfa("<oops") == []


def test_workew_company_from_link():
    xml = """<rss><channel><item><title>SEO Specialist</title>
    <link>https://workew.com/job/seo-specialist-myfitnesspal/</link><description>SEO role The post SEO appeared</description>
    </item></channel></rss>"""
    j = intl.parse_workew(xml)[0]
    assert j.company == "Myfitnesspal" and j.text.endswith("SEO role")


def test_arbeitnow_and_4dayweek_keep_remote_only():
    a = intl.parse_arbeitnow({"data": [
        {"slug": "s1", "title": "SEO Manager", "company_name": "A", "url": "https://a/1", "remote": True,
         "location": "Berlin", "description": "<p>SEO</p>", "created_at": 1794996081, "job_types": ["full time"]},
        {"slug": "s2", "title": "SEO Manager", "company_name": "B", "url": "https://a/2", "remote": False}]})
    assert [j.company for j in a] == ["A"] and a[0].posted_at.year == 2026
    f = intl.parse_4dayweek({"jobs": [
        {"title": "SEO Lead", "company_name": "C", "url": "https://4dayweek.io/job/1", "is_remote": True,
         "location_original": "Worldwide", "description": "SEO", "posted": 1794820601},
        {"title": "SEO Lead", "company_name": "D", "url": "https://4dayweek.io/job/2", "is_remote": False}]})
    assert [j.company for j in f] == ["C"] and f[0].location == "Worldwide"


def test_hn_comment():
    j = intl.parse_hn_comment({"objectID": "42", "comment_text": "Acme | Senior SEO Strategist | REMOTE (anywhere) | Full-time<p>We need SEO help",
                               "created_at": "2026-11-02T10:00:00Z"})
    assert j.company == "Acme" and j.title == "Senior SEO Strategist"
    assert j.url == "https://news.ycombinator.com/item?id=42" and "REMOTE (anywhere)" in j.location


def test_every_feed_has_a_parser_and_name():
    for key in intl.FEEDS:
        assert key in intl.PARSERS and key in intl.FEED_NAMES


ANSWER = json.dumps({"jobs": [
    {"title": "SEO Executive", "company": "Pixel PK", "url": "https://pk.linkedin.com/jobs/view/seo-executive-123",
     "location": "Lahore, Pakistan (Hybrid)", "posted": "2026-11-16", "summary": "SEO role"},
    {"title": "SEO Manager", "company": "Made Up", "url": "https://totally-invented.example/jobs/1", "location": "Remote"},
    {"title": "Office Manager", "company": "X", "url": "https://www.linkedin.com/jobs/view/9"},
    {"title": "SEO Lead", "company": "Old", "url": "https://www.rozee.pk/old-seo-lead-jobs-1", "posted": "2026-09-01"},
    {"title": "SEO Lead", "company": "Search", "url": "https://www.linkedin.com/jobs/search?keywords=seo"},
]})


def test_google_answer_keeps_only_real_seo_posts():
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    out = parse_answer("```json\n" + ANSWER + "\n```", ["linkedin.com", "rozee.pk"], 7, now)
    assert [j["company"] for j in out] == ["Pixel PK"]
    assert grounded("https://pk.linkedin.com/jobs/view/1", ["linkedin.com"])
    assert not grounded("https://evil-linkedin.com.example/x", ["linkedin.com"])
    assert parse_answer("no json here", ["linkedin.com"], 7) == []


class FakeSearchAI:
    can_search = True
    check_model = "m"

    def __init__(self):
        self.calls, self.last_sources = 0, []

    def _call(self, purpose, model, system, content, max_tokens):
        self.calls += 1
        self.last_sources = ["linkedin.com"]
        return ANSWER


class FakeHTTP:
    def get(self, url):
        return SimpleNamespace(status_code=403, text="")


def test_google_scraper_runs_at_most_every_few_hours():
    ai = FakeSearchAI()
    sc = GoogleJobsScraper(ai, client=FakeHTTP(), every_hours=10)
    logs = []
    jobs = sc.run(None, logs.append)
    assert ai.calls == 2 and jobs and jobs[0].source == "google"
    assert jobs[0].extra["region"] == "local" and jobs[0].text.endswith("SEO role")   # page blocked: summary used
    assert sc.run(None, logs.append) == [] and ai.calls == 2                           # not due yet
    assert GoogleJobsScraper(None).run(None, logs.append) == []
    assert "google" in [s.platform for s in build_scrapers(ai)]


class CoverAPI:
    def __init__(self, letter):
        self.letter, self.calls = letter, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        system = json.dumps(kw.get("system"))
        if kw["model"].startswith("claude-haiku"):
            text = json.dumps({"match": True, "reason": "remote SEO", "title": "SEO Lead", "company": "Orbit",
                               "city": "Lahore", "contact_email": "", "company_website": "", "hiring_name": "",
                               "match_score": 88, "eligibility": "worldwide"})
        elif "cover letter" in system:
            text = json.dumps({"letter": self.letter})
        else:
            text = json.dumps({"body": "x"})
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=10,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


def _apply_on_site_job():
    return RawJob(source="linkedin", url="https://www.linkedin.com/jobs/view/77", title="SEO Lead", company="Orbit",
                  location="Lahore (Remote)", text="Remote SEO Lead at a Lahore agency. Apply on our website.",
                  remote_hint=True, extra={"where": "LinkedIn Jobs"})


GOOD = ("Dear Hiring Team,\n\nI'd like to apply for the SEO Lead role at Orbit.\n\nIn 8 years I grew organic traffic "
        "60% for an ecommerce client.\n\nKind regards,\nAhsan Ali")


def test_cover_letter_for_good_apply_on_site_job():
    api = CoverAPI(GOOD)
    pipe = Pipeline(Claude(client=api), None, site_fetcher=lambda u: "")
    assert pipe.process(_apply_on_site_job()) == "no_email"
    job = db.list_jobs()[0]
    assert job["cover_letter"] == GOOD and db.counter("cover_letters") == 1
    cover_call = [c for c in api.calls if "cover letter" in json.dumps(c.get("system"))][0]
    assert "<job_post>" in cover_call["messages"][0]["content"]


def test_cover_letter_with_invented_number_is_dropped():
    api = CoverAPI(GOOD.replace("60%", "400%"))
    pipe = Pipeline(Claude(client=api), None, site_fetcher=lambda u: "")
    assert pipe.process(_apply_on_site_job()) == "no_email"
    assert not db.list_jobs()[0].get("cover_letter")


def test_cover_letter_button():
    api = CoverAPI(GOOD)
    pipe = Pipeline(Claude(client=api), None, site_fetcher=lambda u: "")
    from radar.config import settings
    settings_daily = settings.cover_letter_daily
    try:
        settings.cover_letter_daily = 0          # nothing automatic today...
        pipe.process(_apply_on_site_job())
        jid = db.list_jobs()[0]["id"]
        assert not db.get_job(jid).get("cover_letter")
        assert cover_letter_for_job(jid, Claude(client=api))["cover_letter"] == GOOD   # ...but the button works
    finally:
        settings.cover_letter_daily = settings_daily
