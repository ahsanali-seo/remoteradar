"""LinkedIn Jobs search and LinkedIn content (posts) search. Read only."""
from __future__ import annotations

import re
from urllib.parse import quote

from ..filters import is_seo, parse_relative
from ..models import RawJob
from .base import Scraper, dedupe_list, first, parse_dt, soup, text_of

CITIES = {"Lahore": "Lahore, Punjab, Pakistan", "Islamabad": "Islamabad, Islamabad Capital Territory, Pakistan",
          "Karachi": "Karachi, Sindh, Pakistan", "Rawalpindi": "Rawalpindi, Punjab, Pakistan"}
POST_QUERIES = ["hiring SEO Lahore", "hiring SEO Islamabad", "hiring SEO Karachi", "SEO remote Pakistan"]


def jobs_search_url(city: str) -> str:
    # f_WT=2 → Remote, f_TPR=r604800 → past week, sortBy=DD → newest
    return ("https://www.linkedin.com/jobs/search/?keywords=SEO"
            f"&location={quote(CITIES[city])}&f_WT=2&f_TPR=r604800&sortBy=DD")


def posts_search_url(q: str) -> str:
    return ("https://www.linkedin.com/search/results/content/?keywords=" + quote(q)
            + "&datePosted=%22past-week%22&sortBy=%22date_posted%22")


_JOB_ID = re.compile(r"/jobs/view/(?:[^/?]*-)?(\d+)|currentJobId=(\d+)")


def parse_job_list(html: str) -> list[dict]:
    """Search results page → [{id, title, company, location}]"""
    s = soup(html)
    out = []
    cards = s.select("[data-occludable-job-id], [data-job-id], li.jobs-search-results__list-item, "
                     "div.job-card-container, div.base-card")
    for c in cards:
        jid = c.get("data-occludable-job-id") or c.get("data-job-id")
        a = c.select_one("a[href*='/jobs/view/']")
        if not jid and a:
            m = _JOB_ID.search(a.get("href", ""))
            jid = m and (m.group(1) or m.group(2))
        if not jid:
            continue
        title = text_of(first(c, ".job-card-list__title--link strong", ".job-card-list__title",
                              ".base-search-card__title", "a[href*='/jobs/view/']")).split("\n")[0]
        company = text_of(first(c, ".artdeco-entity-lockup__subtitle", ".job-card-container__primary-description",
                                ".base-search-card__subtitle"))
        loc = text_of(first(c, ".job-card-container__metadata-wrapper", ".artdeco-entity-lockup__caption",
                            ".job-search-card__location"))
        out.append({"id": str(jid), "title": title, "company": company.split("\n")[0], "location": loc})
    seen, uniq = set(), []
    for j in out:
        if j["id"] not in seen:
            seen.add(j["id"])
            uniq.append(j)
    return uniq


def parse_job_view(html: str, job_id: str, hint: dict | None = None) -> RawJob:
    s = soup(html)
    hint = hint or {}
    title = text_of(first(s, "h1.t-24", ".job-details-jobs-unified-top-card__job-title h1",
                          "h1.top-card-layout__title", "h1")) or hint.get("title", "")
    company = text_of(first(s, ".job-details-jobs-unified-top-card__company-name a",
                            ".job-details-jobs-unified-top-card__company-name",
                            "a.topcard__org-name-link", ".topcard__flavor")) or hint.get("company", "")
    tertiary = text_of(first(s, ".job-details-jobs-unified-top-card__tertiary-description-container",
                             ".job-details-jobs-unified-top-card__primary-description-container",
                             ".topcard__flavor--bullet"))
    loc = hint.get("location", "") or tertiary.split("·")[0].strip()
    desc = text_of(first(s, "#job-details", ".jobs-description__content", ".jobs-box__html-content",
                         ".description__text", ".show-more-less-html__markup"))
    posted = None
    t = s.select_one("time[datetime]")
    if t:
        posted = parse_dt(t.get("datetime"))
    if posted is None:
        posted = parse_relative(tertiary)
    workplace = text_of(first(s, ".job-details-preferences-and-skills", ".job-details-fit-level-preferences"))
    return RawJob(
        source="linkedin", url=f"https://www.linkedin.com/jobs/view/{job_id}",
        title=title.split("\n")[0], company=company.split("\n")[0], location=loc,
        text=desc + ("\n" + workplace if workplace else ""), posted_at=posted,
        remote_hint=True, extra={"where": "LinkedIn Jobs"},
    )


def parse_posts(html: str) -> list[RawJob]:
    s = soup(html)
    out = []
    for p in s.select("div.feed-shared-update-v2, div[data-urn^='urn:li:activity'], [data-id^='urn:li:activity']"):
        urn = p.get("data-urn") or p.get("data-id") or ""
        if not urn:
            inner = p.select_one("[data-urn^='urn:li:activity']")
            urn = inner.get("data-urn", "") if inner else ""
        text = text_of(first(p, ".update-components-text", ".feed-shared-update-v2__description",
                             ".feed-shared-text", ".break-words"))
        if not text:
            continue
        author = text_of(first(p, ".update-components-actor__name span[aria-hidden='true']",
                               ".update-components-actor__name", ".feed-shared-actor__name")).split("\n")[0]
        sub = text_of(first(p, ".update-components-actor__sub-description", ".feed-shared-actor__sub-description"))
        link = f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""
        out.append(RawJob(source="linkedin", url=link, text=text, author=author,
                          posted_at=parse_relative(sub), is_social=True,
                          extra={"where": "a LinkedIn post"}))
    return dedupe_list(out)


class LinkedInScraper(Scraper):
    platform = "linkedin"
    name = "LinkedIn"
    browser_profile = "linkedin"

    def run(self, session, log) -> list[RawJob]:
        jobs: list[RawJob] = []
        self.partial = jobs
        # 1. Jobs search
        listed: list[dict] = []
        for city in CITIES:
            session.goto(jobs_search_url(city))
            # The results list is its own scroll pane; scroll the page and the pane.
            session.scroll(3)
            found = parse_job_list(session.html())
            for f in found:
                f.setdefault("location", city)
                if city not in (f.get("location") or ""):
                    f["location"] = (f.get("location") or "") + f" ({city})"
            listed += found
            log(f"Jobs search {city}: {len(found)} results")
        # Only open detail pages for titles that could be SEO (saves page views)
        candidates = [f for f in listed if is_seo(f["title"])] or listed
        half = max(5, session.budget.left // 2)
        for f in candidates[:half]:
            session.goto(f"https://www.linkedin.com/jobs/view/{f['id']}/")
            jobs.append(parse_job_view(session.html(), f["id"], f))
        # 2. Posts search
        for q in POST_QUERIES:
            if session.budget.left <= 0:
                break
            session.goto(posts_search_url(q))
            session.scroll(4)
            posts = parse_posts(session.html())
            jobs += posts
            log(f'Posts "{q}": {len(posts)} scanned')
        return jobs


# ---------------- No-login mode: LinkedIn's public guest job pages ----------------
# These are the same pages anyone sees on linkedin.com/jobs without signing in.
# No account, no cookies. Same pacing, page budget and stop-on-block rules as the browser.

GUEST_SEARCH = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                "?keywords=SEO&location={loc}&f_WT=2%2C3&f_TPR=r604800&sortBy=DD&start={start}")
GUEST_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
GUEST_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def parse_guest_list(html: str) -> list[dict]:
    """Guest search fragment → [{id, title, company, location, posted}]"""
    jobs = parse_job_list(html)
    s = soup(html)
    dates = {}
    for card in s.select("div.base-card, li"):
        urn = card.get("data-entity-urn", "") or ""
        m = re.search(r"jobPosting:(\d+)", urn)
        a = card.select_one("a[href*='/jobs/view/']")
        jid = m.group(1) if m else None
        if not jid and a:
            mm = _JOB_ID.search(a.get("href", ""))
            jid = mm and (mm.group(1) or mm.group(2))
        t = card.select_one("time[datetime]")
        if jid and t:
            dates[str(jid)] = t.get("datetime")
    if not jobs:   # some fragments carry the id only in data-entity-urn
        for card in s.select("div.base-card[data-entity-urn]"):
            m = re.search(r"jobPosting:(\d+)", card.get("data-entity-urn", ""))
            if m:
                jobs.append({"id": m.group(1),
                             "title": text_of(first(card, ".base-search-card__title")),
                             "company": text_of(first(card, ".base-search-card__subtitle")),
                             "location": text_of(first(card, ".job-search-card__location"))})
    for j in jobs:
        j["posted"] = dates.get(j["id"], "")
    return jobs


def parse_guest_job(html: str, job_id: str, hint: dict) -> RawJob:
    job = parse_job_view(html, job_id, hint)
    s = soup(html)
    if not job.title or job.title == hint.get("title", ""):
        job.title = text_of(first(s, "h2.top-card-layout__title", "h1")) or hint.get("title", "")
    crit = text_of(first(s, "ul.description__job-criteria-list"))
    if crit:
        job.text = (job.text + "\n" + crit).strip()
    if job.posted_at is None:
        job.posted_at = parse_dt(hint.get("posted")) or parse_relative(
            text_of(first(s, ".posted-time-ago__text")))
    job.extra = {"where": "LinkedIn Jobs"}
    return job


class LinkedInGuestScraper(Scraper):
    platform = "linkedin"
    name = "LinkedIn (no login)"
    browser_profile = None

    MIN_GAP_HOURS = 3          # LinkedIn at most once every 3 hours, whatever triggers a run
    MAX_DETAILS = 15           # job pages opened per run
    BUDGET = 25                # total LinkedIn page views per run

    def __init__(self, client=None, pause=None, locations=None, region="local", gap_file=None):
        self.client = client
        self.pause = pause
        # 4 cities + "Pakistan" (remote): foreign companies hiring remotely in Pakistan
        self.locations = locations or {**CITIES, "Pakistan": "Pakistan"}
        self.region = region
        self.gap_file = gap_file

    def _get(self, url: str, budget) -> str:
        from .. import db
        from ..guard import Blocked, detect_block, human_pause
        budget.spend()
        (self.pause or human_pause)()
        r = self.client.get(url)
        final = str(getattr(r, "url", url))
        if r.status_code in (429, 999) or r.status_code == 403:
            reason = f"LinkedIn said {r.status_code} (too many requests)"
            db.log(self.platform, f"Stopped for 24h: {reason}", "alert")
            db.block_platform(self.platform, reason, None)
            raise Blocked(reason)
        why = detect_block(final, r.text if r.status_code < 400 else "")
        if why:
            db.log(self.platform, f"Stopped for 24h: {why}", "alert")
            db.block_platform(self.platform, why, None)
            raise Blocked(why)
        return r.text if r.status_code < 400 else ""

    def _gap_path(self):
        from ..config import settings
        return self.gap_file or settings.data_dir / "linkedin_last_run.txt"

    def run(self, session, log) -> list[RawJob]:
        import time as _t
        import httpx
        from .. import db
        from ..dedupe import _h, seen as already_seen
        from ..guard import Budget, BudgetExhausted
        ok, why = db.platform_available("linkedin")
        if not ok:
            log(f"LinkedIn skipped: {why}")
            return []
        gp = self._gap_path()
        try:
            last = float(gp.read_text())
        except Exception:
            last = 0
        if _t.time() - last < self.MIN_GAP_HOURS * 3600:
            mins = int((self.MIN_GAP_HOURS * 3600 - (_t.time() - last)) / 60)
            log(f"LinkedIn rests between runs to avoid limits; next check in about {mins} min")
            return []
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(str(_t.time()))
        if self.pause is None:
            from ..guard import human_pause
            self.pause = lambda: human_pause(6, 15)
        if self.client is None:
            self.client = httpx.Client(timeout=30, follow_redirects=True,
                                       headers={"User-Agent": GUEST_UA,
                                                "Accept-Language": "en-US,en;q=0.9"})
        budget = Budget(self.BUDGET)
        jobs: list[RawJob] = []
        listed: list[dict] = []
        try:
            for city, loc in self.locations.items():
                url = GUEST_SEARCH.format(loc=quote(loc), start=0)
                if loc == "Worldwide":
                    url += "&geoId=92000000"          # LinkedIn's id for "Worldwide"
                found = parse_guest_list(self._get(url, budget))
                for f in found:
                    f["intl"] = city == "Pakistan" or self.region == "international"
                    if not f["intl"] and city not in (f.get("location") or ""):
                        f["location"] = (f.get("location") or "") + f" ({city})"
                listed += found
                log(f"Jobs {city} (public pages): {len(found)} results")
            seen, uniq = set(), []
            for f in listed:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    uniq.append(f)
            # Only open jobs we have never seen: most requests used to go to old jobs
            fresh = [f for f in uniq if is_seo(f["title"])
                     and not already_seen(["url:" + _h(f"https://www.linkedin.com/jobs/view/{f['id']}")])]
            log(f"{len(fresh)} new SEO jobs to open ({len(uniq)} listed)")
            for f in fresh[: self.MAX_DETAILS]:
                j = parse_guest_job(self._get(GUEST_JOB.format(id=f["id"]), budget), f["id"], f)
                if f.get("intl"):
                    if "remote" not in (j.location or "").lower():
                        j.location = f"{j.location or 'Pakistan'} (Remote)"
                    j.extra.update(region="international", where="LinkedIn Pakistan remote")
                    j.source = "linkedin_ww"
                jobs.append(j)
        except BudgetExhausted:
            log(f"Page-view limit ({budget.limit}) reached, stopping for this run")
        return jobs
