from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import RawJob


class Scraper:
    platform: str = ""          # agent card key
    name: str = ""              # label used in the activity feed
    browser_profile: str | None = None   # None = no browser needed

    def run(self, session, log) -> list[RawJob]:  # pragma: no cover - needs a browser
        raise NotImplementedError


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def clean(s: str | None) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", re.sub(r"\n\s*\n+", "\n", s or "")).strip()


def text_of(el) -> str:
    return clean(el.get_text("\n", strip=True)) if el is not None else ""


def first(el, *selectors: str):
    for sel in selectors:
        found = el.select_one(sel)
        if found is not None:
            return found
    return None


def abs_url(base: str, href: str | None) -> str:
    return urljoin(base, href) if href else ""


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v[:10], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def dedupe_list(jobs: list[RawJob]) -> list[RawJob]:
    seen, out = set(), []
    for j in jobs:
        k = j.url or j.text[:120]
        if k in seen:
            continue
        seen.add(k)
        out.append(j)
    return out
