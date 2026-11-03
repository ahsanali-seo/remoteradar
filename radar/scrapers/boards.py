"""Public job boards (Indeed PK, Rozee.pk, Mustakbil, BrightSpyre) and company career pages.
No login. Respects robots.txt. Uses a generic link harvester so small layout changes don't break it."""
from __future__ import annotations

import csv
import re
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..config import settings
from ..filters import city_of, is_seo, parse_relative
from ..models import RawJob
from .base import Scraper, abs_url, clean, first, parse_dt, soup, text_of

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

DEFAULT_BOARDS = {
    "indeed": {"platform": "indeed", "label": "Indeed Pakistan",
               "urls": ["https://pk.indeed.com/jobs?q=SEO&l=Lahore&fromage=7",
                        "https://pk.indeed.com/jobs?q=SEO&l=Islamabad&fromage=7",
                        "https://pk.indeed.com/jobs?q=SEO+remote&l=Pakistan&fromage=7"],
               "job_link": r"/viewjob|/rc/clk|/pagead/clk|jk="},
    "rozee": {"platform": "rozee", "label": "Rozee.pk",
              "urls": ["https://www.rozee.pk/job/jsearch/q/seo%20lahore",
                       "https://www.rozee.pk/job/jsearch/q/seo%20islamabad"],
              "job_link": r"rozee\.pk/.+-jobs?-\d+|/job/"},
    "mustakbil": {"platform": "rozee", "label": "Mustakbil",
                  "urls": ["https://www.mustakbil.com/jobs/search?keywords=seo&location=lahore",
                           "https://www.mustakbil.com/jobs/search?keywords=seo&location=islamabad"],
                  "job_link": r"/jobs/\d+|/job/"},
    "brightspyre": {"platform": "rozee", "label": "BrightSpyre",
                    "urls": ["https://www.brightspyre.com/opening/?keyword=seo&city=Lahore",
                             "https://www.brightspyre.com/opening/?keyword=seo&city=Islamabad"],
                    "job_link": r"/opening/|/job/"},
}


def load_boards() -> dict:
    """sources/job_boards.csv (platform,name,url,link_pattern) replaces the defaults if present.
    Edit that file if a site changes its search URL."""
    p = settings.sources_dir / "job_boards.csv"
    if not p.exists():
        return DEFAULT_BOARDS
    boards: dict = {}
    with p.open(newline="") as f:
        for r in csv.DictReader(l for l in f if l.strip() and not l.startswith("#")):
            name = (r.get("name") or "").strip()
            label = re.sub(r"\s+(Lahore|Islamabad|Karachi|Rawalpindi)$", "", name, flags=re.I) or r["platform"]
            b = boards.setdefault(label, {"platform": r["platform"].strip(), "label": label,
                                          "urls": [], "job_link": r.get("link_pattern") or None})
            b["urls"].append(r["url"].strip())
    return boards


class Robots:
    def __init__(self):
        self.cache: dict[str, RobotFileParser | None] = {}
        self.last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        host = urlsplit(url).netloc
        if host not in self.cache:
            rp = RobotFileParser()
            try:
                r = httpx.get(f"https://{host}/robots.txt", headers={"User-Agent": UA},
                              timeout=15, follow_redirects=True)
                if r.status_code >= 400:
                    self.cache[host] = None       # no robots.txt → allowed
                else:
                    rp.parse(r.text.splitlines())
                    self.cache[host] = rp
            except Exception:
                self.cache[host] = None
        rp = self.cache[host]
        return True if rp is None else rp.can_fetch("*", url) or rp.can_fetch(UA, url)

    def wait(self, url: str, gap: float = 4.0) -> None:
        host = urlsplit(url).netloc
        since = time.time() - self.last.get(host, 0)
        if since < gap:
            time.sleep(gap - since)
        self.last[host] = time.time()


def harvest_links(html: str, base: str, job_link: str | None = None, need_seo: bool = True) -> list[dict]:
    """Find job links on any listing page: anchors whose text (or card) mentions SEO."""
    s = soup(html)
    pat = re.compile(job_link, re.I) if job_link else None
    out, seen = [], set()
    for a in s.select("a[href]"):
        href = a.get("href", "")
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = abs_url(base, href)
        title = clean(a.get_text(" ", strip=True))
        card = a.find_parent(["li", "article", "tr"]) or a.find_parent("div")
        card_text = text_of(card) if card else title
        if len(card_text) > 1500:
            card_text = title
        if pat and not pat.search(url):
            continue
        if need_seo and not (is_seo(title) or (pat and is_seo(card_text[:400]))):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": title[:150], "card": card_text[:1200]})
    return out


def parse_detail(html: str, url: str, source: str, hint: dict, where: str) -> RawJob:
    s = soup(html)
    for bad in s.select("script, style, nav, footer, header, noscript, select, option, form, button, "
                        "iframe, svg, [role=dialog], [class*='modal' i], [class*='breadcrumb' i], "
                        "[class*='similar' i], [class*='related' i], [class*='share' i], [class*='alert' i], "
                        "[class*='popup' i], [class*='cookie' i], [id*='ajax' i], [class*='ajax' i]"):
        bad.decompose()
    title = text_of(first(s, "h1")) or hint.get("title", "")
    company = ""
    m = first(s, "[data-company-name]", "[data-testid='inlineHeader-companyName']", ".company-name",
              ".companyName", "[class*='company' i] a", "meta[property='og:site_name']")
    if m is not None:
        company = clean(m.get("content") or m.get_text(" ", strip=True))
    main = first(s, "#jobDescriptionText", "[itemprop='description']", ".jbody", ".job-dtl", ".jobdetail",
                 "[class*='job-description' i]", "[class*='jobDescription' i]", "[class*='description' i]",
                 "main", "article", "body")
    desc = clean_description(text_of(main))[:12000]
    posted = None
    t = s.select_one("time[datetime]")
    if t:
        posted = parse_dt(t.get("datetime"))
    if posted is None:
        m2 = re.search(r"(posted|active)\s*:?\s*([^\n]{1,30})", desc, re.I)
        if m2:
            posted = parse_relative(m2.group(2))
    loc = city_of(hint.get("card", "") + " " + desc) or ""
    return RawJob(source=source, url=url, title=title.split("\n")[0][:150], company=company[:120],
                  location=loc, text=(hint.get("card", "") + "\n" + desc).strip(), posted_at=posted,
                  extra={"where": where})


_JUNK_LINE = re.compile(r"(?i)^(you have successfully applied|browse similar jobs|this job is saved|saved jobs|"
                        r"error occurred|ajax messages|home|show more|show less|read more|apply now|"
                        r"login|sign ?up|share|report this job|\d{4}|\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|"
                        r"sep|oct|nov|dec)\W*$")


def clean_description(text: str) -> str:
    """Drop menu items, button labels and long runs of numbers (date dropdowns) from scraped text."""
    t = re.sub(r"(\b(19|20)\d{2}\b[\s,]*){4,}", " ", text or "")           # 1990 1991 1992 ...
    t = re.sub(r"(\b\d{1,2}\b[\s,]*){8,}", " ", t)                         # 1 2 3 ... 31
    lines = [l.strip() for l in re.split(r"\n|(?<=[.!?])\s{2,}", t)]
    out = [l for l in lines if l and not _JUNK_LINE.match(l)]
    return re.sub(r"[ \t]+", " ", "\n".join(out)).strip()


def fetch_text(client: httpx.Client, robots: Robots, url: str) -> str | None:
    if not robots.allowed(url):
        return None
    robots.wait(url)
    r = client.get(url)
    if r.status_code >= 400:
        return None
    return r.text


class BoardsScraper(Scraper):
    """One instance per platform card: 'indeed' or 'rozee' (Rozee + Mustakbil + BrightSpyre).
    Uses a clean, logged-out browser (JS-heavy sites), falling back to plain HTTP."""
    browser_profile = "public"

    def __init__(self, platform: str):
        self.platform = platform
        self.name = "Indeed" if platform == "indeed" else "Rozee & boards"
        self.robots = Robots()

    def run(self, session, log) -> list[RawJob]:
        jobs: list[RawJob] = []
        self.partial = jobs
        boards = [b for b in load_boards().values() if b.get("platform") == self.platform]
        for b in boards:
            links: list[dict] = []
            for url in b.get("urls", []):
                if not self.robots.allowed(url):
                    log(f"{b['label']}: robots.txt disallows {url}, skipped")
                    continue
                if session.budget.left <= 0:
                    break
                self.robots.wait(url)
                session.goto(url)
                session.scroll(2)
                found = harvest_links(session.html(), url, b.get("job_link"))
                links += found
            log(f"{b['label']}: {len(links)} SEO listings")
            for h in links[: max(0, session.budget.left)]:
                if not self.robots.allowed(h["url"]):
                    continue
                self.robots.wait(h["url"])
                session.goto(h["url"])
                jobs.append(parse_detail(session.html(), h["url"], self.platform, h, b["label"]))
        return jobs


def read_companies(path=None) -> list[dict]:
    p = path or settings.sources_dir / "companies.csv"
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return [r for r in csv.DictReader(l for l in f if not l.startswith("#")) if r.get("careers_url")]


def parse_careers(html: str, company: dict) -> list[RawJob]:
    """A careers page: every SEO-looking link or heading becomes a candidate."""
    base = company["careers_url"]
    city = company.get("city", "")
    jobs = []
    for h in harvest_links(html, base, None):
        jobs.append(RawJob(source="careers", url=h["url"], title=h["title"],
                           company=company.get("company", ""), location=city,
                           text=h["card"] + f"\nCompany based in {city}.",
                           extra={"where": f"the {company.get('company','')} careers page",
                                  "website": company.get("website", "")}))
    if not jobs:
        s = soup(html)
        for el in s.select("h1, h2, h3, h4, li, strong"):
            t = clean(el.get_text(" ", strip=True))
            if is_seo(t) and len(t) < 160:
                block = text_of(el.find_parent(["section", "article", "div"]) or el)[:1500]
                jobs.append(RawJob(source="careers", url=base, title=t,
                                   company=company.get("company", ""), location=city,
                                   text=block + f"\nCompany based in {city}.",
                                   extra={"where": f"the {company.get('company','')} careers page",
                                          "website": company.get("website", "")}))
                break
    return jobs


class CareersScraper(Scraper):
    platform = "careers"
    name = "Career pages"
    browser_profile = None   # plain HTTP is enough for most agency sites

    def run(self, session, log) -> list[RawJob]:
        companies = read_companies()
        if not companies:
            log("No companies in sources/companies.csv")
            return []
        robots = Robots()
        jobs = []
        with httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True) as client:
            for c in companies:
                try:
                    html = fetch_text(client, robots, c["careers_url"])
                except Exception as e:
                    log(f"{c.get('company')}: {type(e).__name__}")
                    continue
                if html is None:
                    log(f"{c.get('company')}: unavailable or disallowed by robots.txt")
                    continue
                found = parse_careers(html, c)
                jobs += found
                # For roles found as links, open the detail page once
                for j in found:
                    if j.url != c["careers_url"] and robots.allowed(j.url):
                        try:
                            d = fetch_text(client, robots, j.url)
                            if d:
                                det = parse_detail(d, j.url, "careers", {"card": j.text}, j.extra["where"])
                                j.text = det.text
                                j.posted_at = det.posted_at
                        except Exception:
                            pass
        log(f"{len(companies)} career pages checked, {len(jobs)} SEO openings")
        return jobs
