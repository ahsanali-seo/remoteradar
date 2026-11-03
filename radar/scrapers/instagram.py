"""Instagram hashtags and hiring accounts. Captions, plus image text via Haiku if the caption is short."""
from __future__ import annotations

import re

from ..config import settings
from ..models import RawJob
from .base import Scraper, abs_url, clean, parse_dt, soup, text_of

SHORT_CAPTION = 80


def read_targets(path=None) -> list[str]:
    """One hashtag (#seojobs), account (@agency or name) or URL per line.
    Comment lines start with "# " (hash + space) or "//"."""
    p = path or settings.sources_dir / "instagram.txt"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        t = line.strip()
        if not t or t.startswith(("# ", "//")) or t == "#":
            continue
        out.append(t)
    return out


def target_url(t: str) -> str:
    t = t.strip()
    if t.startswith("http"):
        return t
    if t.startswith("#"):
        return f"https://www.instagram.com/explore/tags/{t[1:]}/"
    return f"https://www.instagram.com/{t.lstrip('@')}/"


def parse_grid(html: str, limit: int = 9) -> list[str]:
    s = soup(html)
    links = []
    for a in s.select("a[href*='/p/'], a[href*='/reel/']"):
        href = re.sub(r"\?.*$", "", a.get("href", ""))
        u = abs_url("https://www.instagram.com", href)
        if u not in links:
            links.append(u)
    return links[:limit]


def parse_post(html: str, url: str) -> RawJob:
    s = soup(html)
    caption = ""
    for sel in ["h1", "div._a9zs span", "article span[dir='auto']"]:
        el = s.select_one(sel)
        if el and len(text_of(el)) > len(caption):
            caption = text_of(el)
    if not caption:
        m = s.select_one("meta[property='og:description'], meta[name='description']")
        if m:
            caption = clean(m.get("content", ""))
            caption = re.sub(r'^.*?on Instagram:\s*"?', "", caption).rstrip('"')
    author = ""
    m = s.select_one("meta[property='og:title']")
    if m:
        author = clean(m.get("content", "")).split(" on Instagram")[0]
    t = s.select_one("time[datetime]")
    return RawJob(source="instagram", url=url, text=caption, author=author,
                  posted_at=parse_dt(t.get("datetime")) if t else None, is_social=True,
                  extra={"where": "an Instagram post"})


class InstagramScraper(Scraper):
    platform = "instagram"
    name = "Instagram"
    browser_profile = "instagram"

    def __init__(self, claude=None):
        self.claude = claude

    def run(self, session, log) -> list[RawJob]:
        targets = read_targets()
        if not targets:
            log("Nothing in sources/instagram.txt")
            return []
        jobs = []
        self.partial = jobs
        per = max(3, (session.budget.left - len(targets)) // max(1, len(targets)))
        for t in targets:
            if session.budget.left <= 1:
                log("Page-view limit reached for this run")
                break
            session.goto(target_url(t))
            session.scroll(1)
            links = parse_grid(session.html(), per)
            for u in links:
                if session.budget.left <= 0:
                    break
                session.goto(u)
                job = parse_post(session.html(), u)
                if len(job.text) < SHORT_CAPTION and self.claude is not None:
                    try:
                        img = session.page.locator("article img, main img").first
                        png = img.screenshot(timeout=5000)
                        job.text = (job.text + "\n" + self.claude.read_image(png)).strip()
                        job.extra["image_read"] = True
                    except Exception:
                        pass
                if job.text:
                    jobs.append(job)
            log(f"{t}: {len(links)} posts scanned")
        return jobs
