"""Cloud mode (Hugging Face Spaces): no network, the Hugging Face API is faked."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from radar import auth, cloud, db
from radar.config import settings


@pytest.fixture
def cloudy(tmp_home, monkeypatch):
    monkeypatch.setattr(settings, "cloud", True)
    monkeypatch.setattr(settings, "home", tmp_home)
    monkeypatch.setattr(settings, "token_path", tmp_home / "token.json")
    monkeypatch.setattr(settings, "data_repo", "me/remoteradar-data")
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    monkeypatch.setattr(cloud, "start_scheduler", lambda: None)
    cloud._known.clear()
    cloud._status["restored"] = False
    return tmp_home


def test_locked_without_password(cloudy):
    from radar.server import app
    c = TestClient(app, headers={"X-Radar": "1"})
    r = c.get("/")
    assert r.status_code == 503 and "DASHBOARD_PASSWORD" in r.text
    assert c.get("/api/jobs").status_code == 503
    assert c.get("/healthz").json() == {"ok": True}


def test_password_from_secret_and_login(cloudy, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass-1")
    cloud.apply_password()
    assert auth.enabled()
    first = auth._load()["hash"]
    cloud.apply_password()                      # same password: nothing changes
    assert auth._load()["hash"] == first
    from radar.server import app
    c = TestClient(app, headers={"X-Radar": "1"})
    assert c.get("/api/jobs").status_code == 401
    r = c.post("/api/login", json={"password": "secret-pass-1"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
    monkeypatch.setenv("DASHBOARD_PASSWORD", "another-pass-2")
    cloud.apply_password()                      # changed secret: password changes
    assert auth.login("another-pass-2")


def test_gmail_token_from_secret(cloudy, monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN_JSON", '{"refresh_token": "x"}')
    cloud.write_gmail_token()
    assert json.loads(settings.token_path.read_text()) == {"refresh_token": "x"}
    settings.token_path.unlink()
    monkeypatch.setenv("GMAIL_TOKEN_JSON", "not json")
    cloud.write_gmail_token()
    assert not settings.token_path.exists()


class FakeApi:
    def __init__(self):
        self.commits = []

    def create_commit(self, repo, repo_type, operations, commit_message):
        self.commits.append([(op.path_in_repo, Path(op.path_or_fileobj).read_bytes()) for op in operations])


def test_restore_then_save_only_changes(cloudy, monkeypatch, tmp_path_factory):
    remote = tmp_path_factory.mktemp("remote")
    (remote / "data").mkdir()
    (remote / "profile").mkdir()
    con = sqlite3.connect(remote / "data" / "radar.sqlite3")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    (remote / "profile" / "profile.md").write_text("# Real profile")
    (remote / "data" / "auth.json").write_text("{}")
    (remote / "junk.txt").write_text("not tracked")

    def fake_download(repo, repo_type, local_dir, token):
        import shutil
        shutil.copytree(remote, local_dir, dirs_exist_ok=True)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_download)
    cloud.restore()
    assert cloud._status["restored"]
    assert (cloudy / "profile" / "profile.md").read_text() == "# Real profile"
    assert not (cloudy / "junk.txt").exists()

    api = FakeApi()
    monkeypatch.setattr(cloud, "_api", lambda: api)
    db.set_db_path(cloudy / "data" / "radar.sqlite3")
    db.init()                                   # the database changes; the profile does not
    assert cloud.save("test")
    sent = dict(api.commits[0])
    assert "data/radar.sqlite3" in sent and "profile/profile.md" not in sent
    assert sent["data/radar.sqlite3"].startswith(b"SQLite format 3")
    assert not cloud.save("again")              # nothing changed: no commit
    assert len(api.commits) == 1


def test_no_save_when_restore_failed(cloudy, monkeypatch):
    import huggingface_hub

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    cloud.restore()
    api = FakeApi()
    monkeypatch.setattr(cloud, "_api", lambda: api)
    assert not cloud.save("x")
    assert api.commits == []


def test_next_full_run_is_in_the_future():
    from datetime import datetime
    now = datetime(2026, 11, 3, 23, 30)
    t = cloud._next_full(now)
    assert t > now and t.hour == 9 and t.day == 4
