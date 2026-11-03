from fastapi.testclient import TestClient

from radar import db
from radar.server import app


def test_dashboard_api():
    c = TestClient(app, headers={'X-Radar': '1'})
    assert "Ahsan RemoteRadar" in c.get("/").text
    s = c.get("/api/state").json()
    assert {a["platform"] for a in s["agents"]} >= {"linkedin", "facebook", "instagram", "indeed", "rozee", "careers"}
    jid = db.insert_job({"source": "linkedin", "title": "SEO Specialist", "company": "X", "status": "Drafted",
                         "stage": "drafted"})
    assert c.get("/api/jobs").json()[0]["id"] == jid
    assert c.post(f"/api/jobs/{jid}/status", json={"status": "Sent"}).status_code == 200
    assert db.get_job(jid)["sent_at"]
    assert c.post(f"/api/jobs/{jid}/status", json={"status": "Bogus"}).status_code == 400
    assert "SEO Specialist" in c.get("/api/export.csv").text
    assert c.post("/api/platforms/linkedin/toggle").json() == {"enabled": False}
    assert db.platform_available("linkedin")[0] is False
    c.post("/api/platforms/linkedin/toggle")
    db.block_platform("facebook", "a CAPTCHA appeared", None)
    assert db.get_agent("facebook")["health"] == "Blocked"
    assert db.platform_available("facebook")[0] is False
    c.post("/api/platforms/facebook/clear")
    assert db.platform_available("facebook")[0] is True
    assert c.get("/screenshots/../../etc/passwd").status_code == 404
