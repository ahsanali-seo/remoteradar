"""Facebook groups the spare account already belongs to. Newest posts only. Read only."""
from __future__ import annotations

import re

from ..config import settings
from ..filters import parse_relative
from ..models import RawJob
from .base import Scraper, abs_url, clean, dedupe_list, soup, text_of

_POST_LINK = re.compile(r"/groups/[^/]+/(?:posts|permalink)/\d+|/permalink\.php\?|story_fbid=", re.I)
_TIME_TEXT = re.compile(r"^\s*(\d+\s*(?:m|h|d|w|min|mins|hr|hrs)|just now|yesterday)\b", re.I)


def read_groups(path=None) -> list[str]:
    p = path or settings.sources_dir / "facebook_groups.txt"
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#")]


def group_url_newest(url: str) -> str:
    base = url.split("?")[0].rstrip("/")
    return base + "/?sorting_setting=CHRONOLOGICAL"


def parse_group(html: str, group_url: str) -> list[RawJob]:
    s = soup(html)
    out = []
    for art in s.select("div[role='article']"):
        # Skip comments (nested articles inside a post)
        if art.find_parent("div", attrs={"role": "article"}):
            continue
        msg = art.select_one("div[data-ad-preview='message'], div[data-ad-comet-preview='message']")
        text = text_of(msg) if msg else text_of(art)
        if len(text) < 30:
            continue
        link, posted = "", None
        for a in art.select("a[href]"):
            href = a.get("href", "")
            if _POST_LINK.search(href):
                link = abs_url("https://www.facebook.com", href.split("?__cft__")[0])
                label = a.get("aria-label") or a.get_text(" ", strip=True)
                if _TIME_TEXT.search(label or ""):
                    posted = parse_relative(label)
                break
        if posted is None:
            for span in art.select("a[role='link'] span, abbr"):
                t = span.get_text(" ", strip=True)
                if _TIME_TEXT.search(t):
                    posted = parse_relative(t)
                    break
        author = ""
        h = art.select_one("h2, h3, strong")
        if h:
            author = clean(h.get_text(" ", strip=True))
        out.append(RawJob(source="facebook", url=link or group_url, text=text, author=author,
                          posted_at=posted, is_social=True,
                          extra={"where": "a Facebook jobs group", "group": group_url}))
    return dedupe_list(out)


class FacebookScraper(Scraper):
    platform = "facebook"
    name = "Facebook"
    browser_profile = "facebook"

    def run(self, session, log) -> list[RawJob]:
        groups = read_groups()
        if not groups:
            log("No groups in sources/facebook_groups.txt")
            return []
        jobs = []
        self.partial = jobs
        for g in groups:
            if session.budget.left <= 0:
                log("Page-view limit reached for this run")
                break
            session.goto(group_url_newest(g))
            session.scroll(5)
            # "See more" on long posts is a read action; expand visible ones
            try:
                for btn in session.page.get_by_role("button", name=re.compile(r"^See more$", re.I)).all()[:10]:
                    btn.click(timeout=1500)
            except Exception:
                pass
            posts = parse_group(session.html(), g)
            jobs += posts
            log(f"Group {g.rstrip('/').split('/')[-1]}: {len(posts)} posts scanned")
        return jobs
