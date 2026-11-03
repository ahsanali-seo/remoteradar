"""The read-only page: data is encrypted with the dashboard password's hash."""
import base64
import hashlib
import json
import re

from radar import auth, db, site


def _box(page):
    return json.loads(re.search(r"const BOX=(\{.*?\});", page.read_text()).group(1))


def _open(box, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(box["salt"]), box["iter"])
    return json.loads(AESGCM(key).decrypt(base64.b64decode(box["iv"]), base64.b64decode(box["ct"]), None))


def test_page_opens_only_with_the_dashboard_password(tmp_path):
    auth.set_password("portal-pass-1")
    db.insert_job({"source": "linkedin", "title": "SEO Lead", "company": "Acme", "status": "New",
                   "stage": "new", "text": "Remote SEO lead </script><script>alert(1)</script>"})
    db.insert_job({"source": "rozee", "title": "Hidden", "company": "Nope", "status": "New", "stage": "new"})
    db.block_company("Nope")
    page = site.build(tmp_path / "out")
    html = page.read_text()
    assert "SEO Lead" not in html and "Acme" not in html          # nothing readable without the key
    assert "</script><script>alert" not in html
    data = _open(_box(page), "portal-pass-1")
    assert [j["title"] for j in data["jobs"]] == ["SEO Lead"]       # blocked company left out
    import pytest
    with pytest.raises(Exception):
        _open(_box(page), "wrong-password")


def test_no_password_means_no_data(tmp_path):
    auth.disable()
    db.insert_job({"source": "linkedin", "title": "SEO Lead", "company": "Acme", "status": "New", "stage": "new"})
    html = site.build(tmp_path / "out").read_text()
    assert "const BOX=null;" in html and "Acme" not in html
