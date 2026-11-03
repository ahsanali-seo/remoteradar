from datetime import datetime, timedelta, timezone

from radar.filters import emails_in, free_filter, is_remote, is_seo, parse_relative
from radar.models import RawJob


def test_seo_terms():
    assert is_seo("Hiring Technical SEO Lead")
    assert is_seo("search engine optimization executive")
    assert not is_seo("Seoul office manager")
    assert not is_seo("Graphic designer")


def test_remote_terms():
    assert is_remote("Work from home")
    assert is_remote("Hybrid-remote position")
    assert is_remote("WFH available")
    assert not is_remote("Onsite only, no remote")
    assert is_remote("anything", hint=True)


def test_free_filter_passes_and_fails():
    ok, _ = free_filter(RawJob("x", title="SEO Executive", location="Lahore", text="Remote role"))
    assert ok
    assert free_filter(RawJob("x", title="SEO Executive", location="Karachi", text="Remote"))[1] == "ok"
    assert free_filter(RawJob("x", title="SEO Executive", location="Multan", text="Remote"))[1].startswith("not Lahore")
    assert free_filter(RawJob("x", title="SEO Executive", location="Islamabad", text="Office based"))[1] == "not remote"
    old = datetime.now(timezone.utc) - timedelta(days=9)
    assert "older" in free_filter(RawJob("x", title="SEO", location="Lahore", text="remote", posted_at=old))[1]
    # quick-add only needs to look like SEO
    assert free_filter(RawJob("x", text="SEO job, call me", manual=True))[0]


def test_emails_and_relative_dates():
    assert emails_in("Mail hr@a.pk or HR@a.pk, logo@2x.png") == ["hr@a.pk"]
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    assert parse_relative("3d", now) == now - timedelta(days=3)
    assert parse_relative("2 weeks ago", now) == now - timedelta(weeks=2)
    assert parse_relative("5h •", now) == now - timedelta(hours=5)
    assert parse_relative("Yesterday", now) == now - timedelta(days=1)
    assert parse_relative("nonsense", now) is None
