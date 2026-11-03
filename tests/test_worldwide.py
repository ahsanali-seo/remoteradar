"""Only jobs someone living in Pakistan can actually get."""
from datetime import datetime, timezone

import pytest

from radar.filters import WORLDWIDE_RE, intl_filter, residency_rule
from radar.models import RawJob

NOW = datetime.now(timezone.utc)


def job(text, loc="Remote"):
    return RawJob(source="remoteok", title="Senior SEO Manager", location=loc, text=text, posted_at=NOW,
                  extra={"region": "international", "tags": []})


@pytest.mark.parametrize("text", [
    "Remote in the US. Join our SEO team.",
    "This role is open to US citizens only.",
    "Candidates must be authorized to work in the United States.",
    "You must be located in the UK to apply.",
    "UK residents only, full-time remote.",
    "W-2 employee, benefits included.",
    "You must have the right to work in Canada.",
    "Australian citizens or permanent residents only.",
    "Remote - USA",
])
def test_country_only_jobs_removed(text):
    ok, why = intl_filter(job(text))
    assert ok is False and "only for people living there" in why, text


@pytest.mark.parametrize("text", [
    "US-based agency, we hire SEO contractors worldwide.",
    "Remote. You'll overlap 4 hours with US time zones.",
    "Remote (US hours). Work from anywhere.",
    "We are a fully distributed team across 30 countries.",
    "Our clients are in the UK and US. Fully remote role.",
    "Remote, EST business hours overlap required.",
])
def test_worldwide_or_hours_only_kept(text):
    ok, why = intl_filter(job(text))
    assert ok is True, (text, why)


def test_worldwide_words_beat_a_sponsorship_line():
    t = "We hire globally as contractors. No visa sponsorship (you work from your own country)."
    assert residency_rule(t) and WORLDWIDE_RE.search(t)
    assert intl_filter(job(t))[0] is True


def test_feed_urls_are_lists():
    from radar.scrapers.international import FEEDS
    assert all(isinstance(v, list) and v for v in FEEDS.values())
    assert len(FEEDS["remoteok"]) == 2
