"""Dashboard password."""
import pytest
from fastapi.testclient import TestClient

from radar import auth, server
from radar.config import settings


@pytest.fixture(autouse=True)
def reset():
    auth._fails.clear()
    yield
    auth._fails.clear()


def client():
    return TestClient(server.app, headers={"X-Radar": "1"})


def test_no_password_means_open():
    assert client().get("/api/state").status_code == 200
    assert client().get("/login", follow_redirects=False).status_code == 303


def test_password_protects_everything():
    auth.set_password("Lahore#2026")
    c = client()
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/jobs").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.post("/api/login", json={"password": "wrong-one"}).status_code == 401
    r = c.post("/api/login", json={"password": "Lahore#2026"})
    assert r.status_code == 200 and auth.COOKIE in r.cookies
    assert c.get("/api/state").status_code == 200                     # cookie kept by the client
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/state").status_code == 401


def test_only_a_hash_is_stored():
    auth.set_password("my secret pass")
    raw = (settings.data_dir / "auth.json").read_text()
    assert "my secret pass" not in raw and "hash" in raw


def test_lockout_after_five_wrong_tries():
    auth.set_password("correct horse")
    c = client()
    for _ in range(5):
        c.post("/api/login", json={"password": "nope"})
    r = c.post("/api/login", json={"password": "correct horse"})
    assert r.status_code == 429 and "Wait" in r.json()["detail"]


def test_short_password_rejected_and_off():
    with pytest.raises(ValueError):
        auth.set_password("short")
    auth.set_password("long enough pw")
    assert auth.enabled()
    auth.disable()
    assert not auth.enabled()
    assert client().get("/api/state").status_code == 200


def test_login_photo_is_public_and_optional():
    auth.set_password("Lahore#2026")
    c = client()
    assert c.get("/login-photo").status_code == 404                 # no photo yet: logo shows instead
    (settings.profile_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    assert c.get("/login-photo").status_code == 200                 # visible before logging in
    assert c.get("/api/jobs").status_code == 401                    # everything else still locked
