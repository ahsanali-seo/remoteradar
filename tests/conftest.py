import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    """Every test gets its own database, profile and sources folder."""
    from radar import db
    from radar.config import settings
    for attr, sub in [("profile_dir", "profile"), ("sources_dir", "sources"), ("data_dir", "data"),
                      ("screenshots_dir", "data/screenshots")]:
        p = tmp_path / sub
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, attr, p)
    monkeypatch.setattr(settings, "db_path", tmp_path / "data" / "t.sqlite3")
    db.set_db_path(tmp_path / "data" / "t.sqlite3")
    db.init()
    (tmp_path / "profile" / "profile.md").write_text(
        "# Ahsan Ali\n- Senior SEO Consultant, 8 years\n- Grew organic traffic 60% for an ecommerce client\n"
        "- Tools: Ahrefs, Semrush, GSC\n")
    (tmp_path / "profile" / "template.md").write_text(
        "Dear [Name],\n\nI saw the [Role] role at [Company] on [Where I saw it].\n"
        "[One line about their website or niche]\n\nBest regards,\nAhsan Ali")
    (tmp_path / "profile" / "Ahsan-Ali-SEO-CV.pdf").write_bytes(b"%PDF-1.4 test")
    yield tmp_path
    db.set_db_path(None)
