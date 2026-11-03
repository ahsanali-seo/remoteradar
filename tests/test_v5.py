"""Reposts, four local cities, other Pakistani cities, WhatsApp numbers."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from radar import ai, db, pipeline
from radar.dedupe import fingerprints, remember, seen
from radar.models import RawJob

NOW = datetime.now(timezone.utc)


class AI:
    def check(self, text, meta, region="local"):
        return ai.CheckResult(match=True, reason="ok", title="SEO Manager", company="Acme", city="Karachi",
                              contact_email="", details={"seniority": "senior", "min_years": 5}, match_score=80,
                              eligibility="asia" if region == "international" else "local")

    def find_website(self, *a):
        return ""

    def find_phone(self, *a):
        return ""


def mk(url, city="Karachi", text=None):
    return RawJob(source="rozee", url=url, title="SEO Manager", company="Acme", location=city,
                  text=text or f"Remote SEO Manager in {city}, Pakistan.", posted_at=NOW, remote_hint=True)


def _age_fingerprints(days):
    old = db.iso(db.now() - timedelta(days=days))
    with db.connect() as con:
        con.execute("UPDATE fingerprints SET created_at=?", (old,))


def test_same_link_never_repeats_but_repost_after_21_days_is_new():
    pipe = pipeline.Pipeline(AI(), None, site_fetcher=lambda u: "")
    assert pipe.process(mk("https://rozee.pk/a")) == "no_email"
    assert pipe.process(mk("https://rozee.pk/a")) == "duplicate"           # same post
    assert pipe.process(mk("https://rozee.pk/b")) == "duplicate"           # same role, new link, 2 days later
    first = db.list_jobs()[0]
    db.update_job(first["id"], sent_at=db.iso(), status="Sent")
    _age_fingerprints(25)
    assert pipe.process(mk("https://rozee.pk/a")) == "duplicate"           # old link is still old
    assert pipe.process(mk("https://rozee.pk/c")) == "no_email"            # real repost: new job
    new = [j for j in db.list_jobs() if j["url"].endswith("/c")][0]
    assert new["notes"].startswith("Reposted") and "you applied on" in new["notes"]
    assert pipe.process(mk("https://rozee.pk/d")) == "duplicate"           # window restarted today


def test_karachi_rawalpindi_local_other_cities_international():
    pipe = pipeline.Pipeline(AI(), None, site_fetcher=lambda u: "")
    pipe.process(mk("https://rozee.pk/k", "Karachi"))
    pipe.process(RawJob(source="rozee", url="https://rozee.pk/r", title="SEO Lead", company="Beta",
                        location="Rawalpindi", text="Remote SEO Lead, Rawalpindi", posted_at=NOW, remote_hint=True))
    pipe.process(RawJob(source="rozee", url="https://rozee.pk/m", title="SEO Specialist", company="Gamma",
                        location="Multan", text="Remote SEO Specialist in Multan, Pakistan", posted_at=NOW,
                        remote_hint=True))
    regions = {j["url"][-1]: j["region"] for j in db.list_jobs()}
    assert regions == {"k": "local", "r": "local", "m": "international"}


def test_phone_backfilled_from_old_descriptions_and_found_on_demand(monkeypatch):
    jid = db.insert_job({"source": "rozee", "company": "Acme", "status": "New",
                         "text": "Send CV on WhatsApp 0321-7654321"})
    db.init()
    assert db.get_job(jid)["phone"] == "923217654321"
    from radar import server
    j2 = db.insert_job({"source": "rozee", "company": "Beta", "status": "New", "text": "No number here"})

    class FakeAI:
        def find_phone(self, company, city=""):
            return "923001112233"
    monkeypatch.setattr("radar.ai.make_ai", lambda: FakeAI())
    c = TestClient(server.app, headers={"X-Radar": "1"})
    r = c.post(f"/api/jobs/{j2}/find-phone").json()
    assert r == {"phone": "923001112233", "source": "search"}
    assert c.put(f"/api/jobs/{j2}/phone", json={"phone": "12345"}).status_code == 400
    assert c.put(f"/api/jobs/{j2}/phone", json={"phone": "0333 4445566"}).json()["phone"] == "923334445566"
