"""Paths and settings. Everything lives inside the project folder (RADAR_HOME)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

HOME = Path(os.environ.get("RADAR_HOME", Path(__file__).resolve().parent.parent))

if load_dotenv:
    load_dotenv(HOME / ".env")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    home: Path = HOME
    profile_dir: Path = HOME / "profile"
    sources_dir: Path = HOME / "sources"
    data_dir: Path = HOME / "data"
    browser_dir: Path = HOME / "browser_profiles"
    db_path: Path = HOME / "data" / "radar.sqlite3"
    screenshots_dir: Path = HOME / "data" / "screenshots"
    credentials_path: Path = HOME / "credentials.json"
    token_path: Path = HOME / "token.json"

    check_model: str = os.environ.get("RADAR_CHECK_MODEL", "claude-haiku-5-5")
    write_model: str = os.environ.get("RADAR_WRITE_MODEL", "claude-sonnet-5-5")
    price_check_in: float = _f("PRICE_CHECK_INPUT", 1.0)
    price_check_out: float = _f("PRICE_CHECK_OUTPUT", 5.0)
    price_write_in: float = _f("PRICE_WRITE_INPUT", 3.0)
    price_write_out: float = _f("PRICE_WRITE_OUTPUT", 15.0)

    gmail_address: str = os.environ.get("RADAR_GMAIL", "seowithahsanali@gmail.com")
    gmail_apply_label: bool = _b("GMAIL_APPLY_LABEL", True)
    gmail_label: str = "RemoteRadar/Ready"
    alerts_label: str = "Job Alerts"

    daily_draft_cap: int = _i("DAILY_DRAFT_CAP", 20)
    max_page_views: int = _i("MAX_PAGE_VIEWS_PER_PLATFORM", 40)
    max_age_days: int = _i("MAX_JOB_AGE_DAYS", 7)
    port: int = _i("DASHBOARD_PORT", 8787)
    use_system_chrome: bool = _b("USE_SYSTEM_CHROME", True)

    # ---- Cloud (Hugging Face Spaces) ----
    # RADAR_CLOUD=true: the dashboard listens on all addresses, a password is required,
    # runs are scheduled inside the app, and data is kept in a private Hugging Face dataset.
    cloud: bool = _b("RADAR_CLOUD", False)
    host: str = os.environ.get("RADAR_HOST", "0.0.0.0" if _b("RADAR_CLOUD", False) else "127.0.0.1")
    # Browser without a window. Needed on a server; on the Mac the window stays visible.
    headless: bool = _b("HEADLESS", _b("RADAR_CLOUD", False))
    data_repo: str = os.environ.get("DATA_REPO", "")
    # Sources switched off by setting, e.g. SKIP_SOURCES=indeed (Indeed blocks automated browsers;
    # its jobs come in through Indeed job-alert emails in the Gmail "Job Alerts" label instead).
    skip_sources: tuple = tuple(x.strip().lower() for x in os.environ.get("SKIP_SOURCES", "").split(",")
                                if x.strip())

    # No-login mode (default). LinkedIn Jobs are read from LinkedIn's public guest pages,
    # so no account is ever used. Facebook/Instagram need a login, so they stay off unless
    # you set SOCIAL_LOGIN=true and run `radar login facebook|instagram` with spare accounts.
    linkedin_login: bool = _b("LINKEDIN_LOGIN", False)
    social_login: bool = _b("SOCIAL_LOGIN", False)
    # One Gmail draft listing every job after each run. Off: the dashboard shows them all.
    summary_draft: bool = _b("SUMMARY_DRAFT", False)
    international: bool = _b("INTERNATIONAL", True)
    min_salary_pkr: int = _i("MIN_SALARY_PKR", 100000)
    notify_score: int = _i("NOTIFY_SCORE", 75)
    cv_link: str = os.environ.get("CV_LINK", "")
    portfolio: str = os.environ.get("PORTFOLIO_URL", "https://www.ahsanseo.com")

    # Anti-block pacing
    min_delay: float = 4.0
    max_delay: float = 12.0
    block_pause_hours: int = 24

    @property
    def cv_path(self) -> Path:
        pdfs = sorted(self.profile_dir.glob("*.pdf"))
        named = self.profile_dir / "Ahsan-Ali-SEO-CV.pdf"
        return named if named.exists() else (pdfs[0] if pdfs else named)


settings = Settings()

# Every source shown on the dashboard, in display order.
PLATFORMS = {
    "linkedin": "LinkedIn",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "indeed": "Indeed",
    "rozee": "Rozee & boards",
    "careers": "Career pages",
    "gmail": "Gmail alerts",
    "intl": "International",
    "quickadd": "Quick-add",
}

# Which platforms need a spare account login (radar login <platform>)
LOGIN_PLATFORMS = {
    "linkedin": "https://www.linkedin.com/login",
    "facebook": "https://www.facebook.com/login",
    "instagram": "https://www.instagram.com/accounts/login/",
}
