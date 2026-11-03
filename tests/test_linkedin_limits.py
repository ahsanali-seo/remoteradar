"""LinkedIn must be asked rarely: after the 429 on 30 Oct 2026."""
from pathlib import Path

from radar import db
from radar.dedupe import _h, remember
from radar.scrapers import linkedin

FIX = Path(__file__).parent / "fixtures"


class R:
    def __init__(self, code, text, url="https://www.linkedin.com/jobs-guest/x"):
        self.status_code, self.text, self.url = code, text, url


class Client:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if "seeMoreJobPostings" in url:
            return R(200, (FIX / "linkedin_guest_list.html").read_text())
        return R(200, (FIX / "linkedin_guest_job.html").read_text())


def scraper(c):
    return linkedin.LinkedInGuestScraper(client=c, pause=lambda: None)


def test_blocked_means_zero_requests():
    db.init()
    db.block_platform("linkedin", "LinkedIn said 429", None)
    c = Client()
    assert scraper(c).run(None, lambda m: None) == [] and c.calls == []


def test_three_hour_rest_between_runs():
    db.init()
    c = Client()
    scraper(c).run(None, lambda m: None)
    first = len(c.calls)
    assert first > 0
    scraper(c).run(None, lambda m: None)            # "Run now" again straight away
    assert len(c.calls) == first


def test_already_seen_jobs_are_not_opened():
    db.init()
    remember(["url:" + _h("https://www.linkedin.com/jobs/view/4022222222")], None)
    c = Client()
    jobs = scraper(c).run(None, lambda m: None)
    assert jobs == [] and not any("jobPosting/" in u for u in c.calls)


def test_pakistan_search_tagged_international_and_budget():
    db.init()
    c = Client()
    s = scraper(c)
    s.locations = {"Pakistan": "Pakistan"}
    jobs = s.run(None, lambda m: None)
    assert jobs and jobs[0].source == "linkedin_ww" and jobs[0].extra["region"] == "international"
    assert "(Remote)" in jobs[0].location
    assert s.BUDGET <= 25 and s.MAX_DETAILS <= 15
    assert set(linkedin.LinkedInGuestScraper().locations) == {"Lahore", "Islamabad", "Karachi", "Rawalpindi",
                                                              "Pakistan"}


def test_30_minute_check_never_touches_linkedin(monkeypatch):
    from radar.scrapers import international as intl
    monkeypatch.setattr(intl, "FEEDS", {})
    monkeypatch.setattr(intl, "read_companies", lambda: [])
    called = []
    monkeypatch.setattr(linkedin.LinkedInGuestScraper, "run", lambda self, s, l: called.append(1) or [])
    intl.InternationalScraper(client=object(), pause=0).run(None, lambda m: None)
    assert called == []
