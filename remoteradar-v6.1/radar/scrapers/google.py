"""Google search for SEO jobs, through Gemini's Google Search tool (free tier, no scraping).

Some jobs never reach the feeds we read: LinkedIn and Indeed posts, Rozee.pk ads, company
career pages. Gemini searches Google for them twice a day at most and returns a list.

Links are only kept when their site is one Google really returned for that search (Gemini's
"grounding" list), so a made-up link is dropped. The post page is then opened to read the full
text; when a site blocks that (Indeed often does), the short summary from the search is used.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..config import settings
from ..filters import city_of, is_seo
from ..models import RawJob
from .base import Scraper, parse_dt, soup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 RemoteRadar/1.0 (personal job search)")

SEARCHES = {
    "local": ("Use Google Search to find up to 20 real job postings published in the last {days} days for SEO roles "
              "(SEO Specialist, SEO Executive, SEO Manager, SEO Lead, Technical SEO, Local SEO, SEO Strategist) at "
              "companies in Lahore, Islamabad, Karachi or Rawalpindi, Pakistan, that are remote or hybrid. "
              "Look on LinkedIn Jobs, Indeed Pakistan, Rozee.pk, Mustakbil, Glassdoor and company career pages."),
    "international": ("Use Google Search to find up to 20 real remote job postings published in the last {days} days "
                      "for SEO roles (SEO Specialist, SEO Manager, SEO Lead, Technical SEO, SEO Strategist, "
                      "SEO Content Strategist) that hire worldwide, from anywhere, or from Pakistan or Asia. "
                      "Skip jobs limited to residents of one country (for example 'US only'). "
                      "Look on LinkedIn Jobs, Indeed, Glassdoor, Wellfound and company career pages."),
}
ANSWER = (" Only include postings you actually saw in the search results, with the direct link to the posting "
          "page (not a search results page). Reply with JSON only, no other text: "
          '{{"jobs": [{{"title": "", "company": "", "url": "", "location": "", '
          '"posted": "YYYY-MM-DD or empty", "summary": "2-3 sentences from the posting"}}]}}')
LAST_RUN = "google_last_run.txt"
NOT_A_POST = re.compile(r"(/search|/jobs/?$|/jobs\?|[?&](q|keywords)=)", re.I)


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def grounded(url: str, sources: list[str]) -> bool:
    """True when the link's site is one of the sites Google returned (titles look like 'linkedin.com')."""
    h = _host(url)
    if not h:
        return False
    for s in sources:
        s = (s or "").lower().strip().removeprefix("www.")
        if s and (h == s or h.endswith("." + s) or s.endswith("." + h)):
            return True
    return False


def parse_answer(text: str, sources: list[str], days: int, now: datetime | None = None) -> list[dict]:
    """The AI's JSON -> clean job dicts. Pure, so tests can run on a sample answer."""
    from ..ai import parse_json
    try:
        data = parse_json(text)
    except (ValueError, json.JSONDecodeError):
        return []
    now = now or datetime.now(timezone.utc)
    out, seen = [], set()
    for j in data.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        url = str(j.get("url") or "").strip()
        title = str(j.get("title") or "").strip()
        if not url.startswith("http") or url in seen or NOT_A_POST.search(url):
            continue
        if not is_seo(title) or not grounded(url, sources):
            continue
        posted = parse_dt(str(j.get("posted") or "")) if j.get("posted") else None
        if posted and (now - posted).days > days:
            continue
        seen.add(url)
        out.append({"title": title[:160], "company": str(j.get("company") or "").strip()[:120], "url": url,
                    "location": str(j.get("location") or "").strip()[:200], "posted": posted,
                    "summary": str(j.get("summary") or "").strip()[:2000]})
    return out


class GoogleJobsScraper(Scraper):
    platform = "google"
    name = "Google search"
    browser_profile = None

    def __init__(self, ai=None, client=None, every_hours: float | None = None):
        import os
        self.ai = ai
        self.client = client
        self.every = every_hours if every_hours is not None else float(os.environ.get("GOOGLE_SEARCH_EVERY_HOURS", "10"))
        self.stamp = settings.data_dir / LAST_RUN

    def _due(self) -> bool:
        try:
            return time.time() - float(self.stamp.read_text().strip()) >= self.every * 3600
        except Exception:
            return True

    def _page_text(self, url: str) -> str:
        try:
            r = self.client.get(url)
            if r.status_code >= 400:
                return ""
            s = soup(r.text)
            for t in s(["script", "style", "nav", "header", "footer", "noscript"]):
                t.decompose()
            return re.sub(r"\s+", " ", s.get_text(" ")).strip()[:15000]
        except Exception:
            return ""

    def run(self, session, log) -> list[RawJob]:
        if self.ai is None or not getattr(self.ai, "can_search", False):
            log("Google search needs Gemini (RADAR_AI=gemini): skipped")
            return []
        if not self._due():
            log(f"Google search: already done in the last {self.every:g} hours, skipped this run")
            return []
        import httpx
        if self.client is None:
            self.client = httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA})
        days = settings.max_age_days
        jobs: list[RawJob] = []
        worked = 0
        for region, ask in SEARCHES.items():
            self.ai.last_sources = []
            try:
                out = self.ai._call("search", self.ai.check_model, "You find real, current job postings.",
                                    ask.format(days=days) + ANSWER, 4000)
            except Exception as e:
                log(f"Google search ({region}) failed: {str(e)[:400]}")
                continue
            worked += 1
            sources = list(getattr(self.ai, "last_sources", []) or [])
            found = parse_answer(out, sources, days * 2 if region == "international" else days)
            for j in found:
                page = self._page_text(j["url"])
                text = page if len(page) > 400 and is_seo(page) else j["summary"]
                loc = j["location"] or ("Pakistan" if region == "local" else "Remote")
                reg = "local" if region == "local" and city_of(loc + " " + text[:3000]) else "international"
                jobs.append(RawJob(source="google", url=j["url"], title=j["title"], company=j["company"],
                                   location=loc, text=(f"Location: {loc}\n\n" + text)[:20000],
                                   posted_at=j["posted"], remote_hint=bool(re.search(r"(?i)remote|hybrid|anywhere", loc)),
                                   extra={"region": reg, "where": f"a Google search ({_host(j['url'])})"}))
            log(f"Google search ({region}): {len(found)} SEO posts with checked links "
                f"(Google returned {len(sources)} sites)")
        if not worked:
            return jobs           # nothing searched: try again on the next run instead of waiting 10 hours
        try:
            self.stamp.parent.mkdir(parents=True, exist_ok=True)
            self.stamp.write_text(str(time.time()))
        except Exception:
            pass
        return jobs
