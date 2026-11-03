from radar.dedupe import fingerprints, normalize_url, remember, seen
from radar.models import RawJob


def test_normalize_url():
    assert normalize_url("https://www.linkedin.com/jobs/view/seo-specialist-at-x-4011111111/?trk=abc") == \
        "https://www.linkedin.com/jobs/view/4011111111"
    assert normalize_url("https://www.linkedin.com/jobs/search/?currentJobId=42&keywords=seo") == \
        "https://www.linkedin.com/jobs/view/42"
    assert normalize_url("https://m.facebook.com/groups/a/posts/1/?fbclid=zz&utm_source=x") == \
        "https://www.facebook.com/groups/a/posts/1"
    assert normalize_url("https://pk.indeed.com/viewjob?jk=abc&from=serp&vjs=3") == \
        "https://pk.indeed.com/viewjob?jk=abc&vjs=3"


def test_same_job_on_two_sites_is_one_job():
    a = RawJob("linkedin", url="https://linkedin.com/jobs/view/1", title="SEO Specialist", company="Pixel Orbit")
    b = RawJob("indeed", url="https://pk.indeed.com/viewjob?jk=9", title="SEO  specialist", company="PIXEL ORBIT")
    remember(fingerprints(a), 1)
    assert seen(fingerprints(b))


def test_social_text_hash():
    t = "We are hiring an SEO executive for remote work in Lahore, please send your CV now to us"
    a = RawJob("facebook", url="https://facebook.com/groups/x/posts/1", text=t, is_social=True)
    b = RawJob("instagram", url="https://instagram.com/p/abc", text=t + " #jobs", is_social=True)
    remember(fingerprints(a), 1)
    assert seen(fingerprints(b))
