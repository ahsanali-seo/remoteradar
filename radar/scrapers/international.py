"""International remote jobs, no login anywhere.

1. Remote job sites with public feeds: Remotive, RemoteOK, We Work Remotely, Himalayas, Jobicy,
   Working Nomads. Each feed's parser is a pure function so tests can run on sample data, and
   each feed fails on its own without stopping the others.
2. Company career boards (sources/companies_intl.csv). Most companies post jobs through an
   applicant-tracking system with a public job list: Greenhouse, Lever, Ashby, Workable or
   SmartRecruiters. The tool finds which one a company uses and remembers it.
3. LinkedIn "Pakistan + Remote" is fetched by the LinkedIn agent (4 times a day), not here.

Every job is tagged region="international". Feeds change their format sometimes; parsers accept
several field names and skip anything they can't read.
"""
from __future__ import annotations

import csv
import html as htmllib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..config import settings
from ..filters import is_seo
from ..models import RawJob
from .base import Scraper, parse_dt, soup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 RemoteRadar/1.0 (personal job search)")

FEEDS = {   # several URLs per site: SEO search + the wider marketing list (titles are filtered for SEO)
    "remotive": ["https://remotive.com/api/remote-jobs?search=seo&limit=100",
                 "https://remotive.com/api/remote-jobs?category=marketing&limit=200"],
    "remoteok": ["https://remoteok.com/api?tag=seo", "https://remoteok.com/api?tag=marketing"],
    "wwr": ["https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss"],
    "himalayas": ["https://himalayas.app/jobs/api/search?q=seo&limit=100",
                  "https://himalayas.app/jobs/api/search?q=search%20engine%20optimization&limit=100"],
    "jobicy": ["https://jobicy.com/api/v2/remote-jobs?count=100&tag=seo",
               "https://jobicy.com/api/v2/remote-jobs?count=100&industry=marketing"],
    "workingnomads": ["https://www.workingnomads.com/api/exposed_jobs/"],
}
FEED_NAMES = {"remotive": "Remotive", "remoteok": "RemoteOK", "wwr": "We Work Remotely",
              "himalayas": "Himalayas", "jobicy": "Jobicy", "workingnomads": "Working Nomads",
              "ats": "Company careers", "linkedin_ww": "LinkedIn Pakistan remote"}


# ---------------- helpers ----------------
def _text(html_or_text: str) -> str:
    if not html_or_text:
        return ""
    t = htmllib.unescape(html_or_text)
    if "<" in t and ">" in t:
        s = soup(t)
        for br in s.find_all(["br", "p", "li", "h1", "h2", "h3", "h4", "div"]):
            br.insert_after("\n")
        t = s.get_text(" ")
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def _when(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 10_000_000_000 else v
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(v).strip()
    if s.isdigit():
        return _when(int(s))
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    return parse_dt(s.replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} \d", s) else s)


def _pick(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _salary(lo, hi, cur="USD") -> str:
    try:
        lo, hi = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    fmt = lambda n: f"{n:,}"  # noqa: E731
    return f"{cur} {fmt(lo)}–{fmt(hi)}" if lo and hi else f"{cur} {fmt(lo or hi)}"


def _job(source, title, company, url, location, text, posted, salary="", job_type="", tags=None,
         ats=False, website="") -> RawJob:
    head = []
    if salary:
        head.append(f"Salary: {salary}")
    if job_type:
        head.append(f"Job type: {job_type}")
    if location:
        head.append(f"Location: {location}")
    body = ("\n".join(head) + "\n\n" + text).strip()
    return RawJob(source=source, url=url, title=(title or "").strip()[:160], company=(company or "").strip()[:120],
                  location=(location or "Remote").strip()[:200], text=body[:20000], posted_at=posted,
                  remote_hint=True,
                  extra={"region": "international", "where": FEED_NAMES.get(source, source),
                         "tags": [str(t) for t in (tags or [])][:20], "ats": ats, "website": website})


# ---------------- feed parsers (pure) ----------------
def parse_remotive(data) -> list[RawJob]:
    out = []
    for j in (data or {}).get("jobs", []):
        out.append(_job("remotive", j.get("title"), j.get("company_name"), j.get("url"),
                        j.get("candidate_required_location"), _text(j.get("description")),
                        _when(j.get("publication_date")), j.get("salary", ""), j.get("job_type", ""),
                        j.get("tags")))
    return out


def parse_remoteok(data) -> list[RawJob]:
    out = []
    for j in data or []:
        if not isinstance(j, dict) or not j.get("position"):
            continue                                   # first item is a legal notice
        out.append(_job("remoteok", j.get("position"), j.get("company"), _pick(j, "url", "apply_url"),
                        j.get("location") or "Worldwide", _text(j.get("description")),
                        _when(_pick(j, "epoch", "date")), _salary(j.get("salary_min"), j.get("salary_max")),
                        "", j.get("tags")))
    return out


def parse_wwr(xml_text: str) -> list[RawJob]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        raw_title = g("title")
        company, _, title = raw_title.partition(":")
        if not title:
            company, title = "", raw_title
        out.append(_job("wwr", title.strip(), company.strip(), g("link"), g("region") or "Anywhere",
                        _text(g("description")), _when(g("pubDate")), "", g("type"), [g("category")]))
    return out


def parse_himalayas(data) -> list[RawJob]:
    out = []
    for j in (data or {}).get("jobs", []):
        locs = j.get("locationRestrictions") or []
        loc = ", ".join(l if isinstance(l, str) else l.get("name", "") for l in locs) or "Worldwide"
        out.append(_job("himalayas", j.get("title"), _pick(j, "companyName", "company"),
                        _pick(j, "applicationLink", "guid", "url"), loc,
                        _text(_pick(j, "description", "excerpt")), _when(_pick(j, "pubDate", "publishedAt")),
                        _salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency") or "USD"),
                        j.get("employmentType", ""), j.get("categories")))
    return out


def parse_jobicy(data) -> list[RawJob]:
    out = []
    for j in (data or {}).get("jobs", []):
        jt = j.get("jobType")
        out.append(_job("jobicy", j.get("jobTitle"), j.get("companyName"), j.get("url"),
                        j.get("jobGeo") or "Anywhere", _text(_pick(j, "jobDescription", "jobExcerpt")),
                        _when(j.get("pubDate")),
                        _salary(j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency") or "USD"),
                        ", ".join(jt) if isinstance(jt, list) else (jt or ""), j.get("jobIndustry")))
    return out


def parse_workingnomads(data) -> list[RawJob]:
    out = []
    for j in data or []:
        if not isinstance(j, dict):
            continue
        tags = j.get("tags") or ""
        tags = [t.strip() for t in tags.split(",")] if isinstance(tags, str) else tags
        out.append(_job("workingnomads", j.get("title"), j.get("company_name"), j.get("url"),
                        j.get("location") or "Anywhere", _text(j.get("description")),
                        _when(j.get("pub_date")), "", "", tags + [j.get("category_name", "")]))
    return out


PARSERS = {"remotive": parse_remotive, "remoteok": parse_remoteok, "wwr": parse_wwr,
           "himalayas": parse_himalayas, "jobicy": parse_jobicy, "workingnomads": parse_workingnomads}


# ---------------- company boards (ATS) ----------------
ATS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
}


def parse_ats(ats: str, data, company: str, slug: str) -> list[RawJob]:
    out = []
    if ats == "greenhouse":
        for j in (data or {}).get("jobs", []):
            out.append(_job("ats", j.get("title"), company, j.get("absolute_url"),
                            (j.get("location") or {}).get("name", ""), _text(j.get("content")),
                            _when(j.get("updated_at")), ats=True))
    elif ats == "lever":
        for j in data or []:
            c = j.get("categories") or {}
            lists = "\n".join(f"{l.get('text', '')}\n{_text(l.get('content', ''))}" for l in j.get("lists") or [])
            loc = c.get("location", "")
            if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
                loc = f"Remote ({loc})" if loc else "Remote"
            out.append(_job("ats", j.get("text"), company, j.get("hostedUrl"), loc,
                            (j.get("descriptionPlain") or "") + "\n" + lists, _when(j.get("createdAt")),
                            "", c.get("commitment", ""), [c.get("team", "")], ats=True))
    elif ats == "ashby":
        for j in (data or {}).get("jobs", []):
            loc = j.get("location", "")
            if j.get("isRemote") and "remote" not in loc.lower():
                loc = f"Remote ({loc})" if loc else "Remote"
            comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
            out.append(_job("ats", j.get("title"), company, j.get("jobUrl"), loc,
                            j.get("descriptionPlain") or _text(j.get("descriptionHtml")),
                            _when(j.get("publishedAt")), comp, j.get("employmentType", ""), ats=True))
    elif ats == "workable":
        for j in (data or {}).get("jobs", []):
            loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
            if j.get("telecommuting"):
                loc = f"Remote ({loc})" if loc else "Remote"
            out.append(_job("ats", j.get("title"), company, j.get("url") or j.get("shortlink"), loc,
                            _text(j.get("description")), _when(_pick(j, "published_on", "created_at")),
                            "", j.get("employment_type", ""), ats=True))
    elif ats == "smartrecruiters":
        for j in (data or {}).get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(x for x in [l.get("city"), l.get("country")] if x)
            if l.get("remote"):
                loc = f"Remote ({loc})" if loc else "Remote"
            out.append(_job("ats", j.get("name"), company,
                            f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}", loc, j.get("name", ""),
                            _when(j.get("releasedDate")), ats=True))
    return out


def read_companies(path=None) -> list[dict]:
    p = path or settings.sources_dir / "companies_intl.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("company") or "").strip()
                and not r["company"].lstrip().startswith("#")]


def slug_candidates(company: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", company.lower()).strip()
    words = base.split()
    c = ["".join(words), "-".join(words), "".join(words) + ".com", "".join(words) + "hq"]
    if words and words[0] != "".join(words):
        c.append(words[0])
    return list(dict.fromkeys(x for x in c if x))


def _has_jobs(ats: str, d) -> bool:
    if ats == "lever":
        return isinstance(d, list) and len(d) > 0
    if ats == "smartrecruiters":
        return isinstance(d, dict) and len(d.get("content") or []) > 0
    return isinstance(d, dict) and len(d.get("jobs") or []) > 0


# ---------------- the scraper ----------------
class InternationalScraper(Scraper):
    platform = "intl"
    name = "International"
    browser_profile = None

    def __init__(self, client=None, pause: float = 1.5, cache_path=None):
        self.client = client
        self.pause = pause
        self.cache_path = cache_path or settings.data_dir / "ats_cache.json"

    def _get(self, url: str):
        time.sleep(self.pause)
        r = self.client.get(url)
        if r.status_code >= 400:
            return None
        return r

    def _cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text())
        except Exception:
            return {}

    def run(self, session, log) -> list[RawJob]:
        import httpx
        if self.client is None:
            self.client = httpx.Client(timeout=30, follow_redirects=True,
                                       headers={"User-Agent": UA, "Accept": "application/json, text/xml, */*"})
        jobs: list[RawJob] = []
        # 1. remote job sites
        from ..filters import intl_filter
        for key, urls in FEEDS.items():
            found, seen_urls, reached = [], set(), False
            for url in urls:
                try:
                    r = self._get(url)
                    if r is None:
                        continue
                    reached = True
                    data = r.text if key == "wwr" else r.json()
                    for j in PARSERS[key](data):
                        if is_seo(j.title) and j.url not in seen_urls:
                            seen_urls.add(j.url)
                            found.append(j)
                except Exception as e:
                    log(f"{FEED_NAMES[key]} failed: {type(e).__name__}")
            if not reached:
                log(f"{FEED_NAMES[key]}: not reachable right now")
                continue
            open_ = sum(1 for j in found if intl_filter(j)[0])
            jobs += found
            log(f"{FEED_NAMES[key]}: {len(found)} SEO jobs, {open_} open to Pakistan")
        # 2. company boards
        cache = self._cache()
        for row in read_companies():
            company = row["company"].strip()
            ats, slug = (row.get("ats") or "").strip().lower(), (row.get("slug") or "").strip()
            if not ats and company in cache and cache[company].get("v") == 2:
                c = cache[company]
                if c.get("ats") == "none" and time.time() - c.get("checked", 0) < 7 * 86400:
                    continue                                 # looked last week, nothing found
                if c.get("ats") != "none":
                    ats, slug = c.get("ats", ""), c.get("slug", "")
            try:
                data = None
                if ats and slug and ats in ATS:
                    r = self._get(ATS[ats].format(slug=slug))
                    data = r.json() if r is not None else None
                else:
                    for cand in slug_candidates(company):
                        for name, tmpl in ATS.items():
                            r = self._get(tmpl.format(slug=cand))
                            if r is None:
                                continue
                            try:
                                d = r.json()
                            except Exception:
                                continue
                            if _has_jobs(name, d):          # an empty board is usually the wrong company
                                ats, slug, data = name, cand, d
                                break
                        if data is not None:
                            break
                    cache[company] = {"ats": ats or "none", "slug": slug, "v": 2, "checked": time.time()}
                if data is None:
                    log(f"{company}: careers board not found (add ats,slug in companies_intl.csv)")
                    continue
                all_jobs = parse_ats(ats, data, company, slug)
                found = [j for j in all_jobs if is_seo(j.title)]
                jobs += found
                log(f"{company} ({ats}): {len(all_jobs)} open roles, {len(found)} SEO")
            except Exception as e:
                log(f"{company} failed: {type(e).__name__}")
        try:
            self.cache_path.write_text(json.dumps(cache, indent=1))
        except Exception:
            pass
        # LinkedIn (incl. "Pakistan remote") runs only in the full runs, from the LinkedIn agent,
        # never from this 30-minute check, so LinkedIn is not asked too often.
        return jobs
