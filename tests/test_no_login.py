"""No-login mode: LinkedIn public guest pages, Facebook/Instagram off by default."""
from pathlib import Path

import pytest

from radar.guard import Blocked
from radar.scrapers import linkedin

FIX = Path(__file__).parent / "fixtures"


def fx(n):
    return (FIX / n).read_text()


class R:
    def __init__(self, code, text, url="https://www.linkedin.com/jobs-guest/x"):
        self.status_code, self.text, self.url = code, text, url


class Client:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return R(404, "")


def test_guest_list_parse():
    jobs = linkedin.parse_guest_list(fx("linkedin_guest_list.html"))
    assert [j["id"] for j in jobs] == ["4022222222", "4033333333"]
    assert jobs[0]["title"] == "SEO Specialist" and jobs[0]["company"] == "Pixel Peak Digital"
    assert jobs[0]["posted"] == "2026-10-28"


def test_guest_scraper_end_to_end():
    c = Client({"seeMoreJobPostings": R(200, fx("linkedin_guest_list.html")),
                "jobPosting/4022222222": R(200, fx("linkedin_guest_job.html"))})
    s = linkedin.LinkedInGuestScraper(client=c, pause=lambda: None)
    jobs = s.run(None, lambda m: None)
    assert len(jobs) == 1                         # accountant never opened
    j = jobs[0]
    assert j.title == "SEO Specialist" and j.company == "Pixel Peak Digital"
    assert "careers@pixelpeak.pk" in j.text and j.url.endswith("/jobs/view/4022222222")
    assert j.posted_at is not None
    assert not any("4033333333" in u for u in c.calls)
    assert all("jobs-guest" in u for u in c.calls)   # never touches login pages


@pytest.mark.parametrize("code", [429, 999])
def test_guest_stops_on_rate_limit(code):
    from radar import db
    db.init()
    c = Client({"seeMoreJobPostings": R(code, "")})
    with pytest.raises(Blocked):
        linkedin.LinkedInGuestScraper(client=c, pause=lambda: None).run(None, lambda m: None)
    assert len(c.calls) == 1
    ok, why = db.platform_available("linkedin")
    assert not ok


def test_guest_stops_on_authwall():
    from radar import db
    db.init()
    c = Client({"seeMoreJobPostings": R(200, "", url="https://www.linkedin.com/authwall?x")})
    with pytest.raises(Blocked):
        linkedin.LinkedInGuestScraper(client=c, pause=lambda: None).run(None, lambda m: None)


def test_default_sources_need_no_login():
    from radar.pipeline import build_scrapers, no_login_off
    sc = build_scrapers()
    names = [s.platform for s in sc]
    assert "facebook" not in names and "instagram" not in names
    assert isinstance(sc[0], linkedin.LinkedInGuestScraper) and sc[0].browser_profile is None
    assert no_login_off() == ["facebook", "instagram"]
