"""Fetch a company's homepage summary (title, meta description, H1, first 300 words)."""
from __future__ import annotations

import re

import httpx

from .scrapers.base import clean, soup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com",
             "aol.com", "proton.me", "protonmail.com", "ymail.com", "msn.com"}


def website_for(email: str, website: str) -> str:
    if website:
        w = website.strip()
        return w if w.startswith("http") else "https://" + w.lstrip("/")
    dom = email.split("@")[-1].lower() if "@" in email else ""
    if dom and dom not in FREE_MAIL:
        return "https://" + dom
    return ""


def summarize_html(html: str) -> str:
    s = soup(html)
    title = clean(s.title.get_text()) if s.title else ""
    m = s.select_one("meta[name='description'], meta[property='og:description']")
    desc = clean(m.get("content", "")) if m else ""
    h1 = clean(s.h1.get_text(" ", strip=True)) if s.h1 else ""
    for bad in s.select("script, style, nav, footer, header, noscript, svg"):
        bad.decompose()
    body = s.body.get_text(" ", strip=True) if s.body else ""
    words = re.sub(r"\s+", " ", body).split()[:300]
    return f"Title: {title}\nDescription: {desc}\nH1: {h1}\nText: {' '.join(words)}"


def fetch_site(url: str, client: httpx.Client | None = None) -> str:
    if not url:
        return ""
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    try:
        r = client.get(url)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
            return ""
        return summarize_html(r.text)
    except Exception:
        return ""
    finally:
        if own:
            client.close()


# ---------------- find a company's hiring email on its own website ----------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CONTACT_PATHS = ["", "contact", "contact-us", "careers", "jobs", "about", "about-us"]
_BAD_EMAIL = re.compile(r"(noreply|no-reply|donotreply|example\.|sentry|wixpress|\.png$|\.jpg$|\.jpeg$"
                        r"|\.gif$|\.webp$|\.svg$|@2x|domain\.com|email\.com|yourname)", re.I)
_RANK = [(re.compile(r"^(careers?|jobs?|hr|recruit\w*|talent|hiring|people|work)\b", re.I), 0),
         (re.compile(r"^(hello|hi|info|contact|team|office|admin)\b", re.I), 1)]


def _rank(addr: str) -> int:
    local = addr.split("@")[0]
    for rx, r in _RANK:
        if rx.search(local):
            return r
    return 2


def normalize_email(e: str) -> str:
    """'Info@WWW.Company.com.' -> 'info@company.com'"""
    e = (e or "").strip().strip(".,;:<>()[]").lower()
    if "@" in e:
        local, dom = e.split("@", 1)
        dom = dom.removeprefix("www.")
        e = f"{local}@{dom}"
    return e


def emails_on_page(html: str, site_domain: str) -> list[str]:
    s = soup(html)
    found = [a["href"][7:].split("?")[0] for a in s.select("a[href^='mailto:']")]
    text = s.get_text(" ", strip=True) if s else ""
    found += EMAIL_RE.findall(text)
    out = []
    dom = site_domain.lower().removeprefix("www.")
    for e in found:
        e = normalize_email(e)
        if not EMAIL_RE.fullmatch(e) or _BAD_EMAIL.search(e):
            continue
        edom = e.split("@")[1]
        # Keep the company's own addresses, plus free-mail ones they printed themselves.
        if dom and not (edom == dom or edom.endswith("." + dom) or dom.endswith("." + edom)
                        or edom in FREE_MAIL):
            continue
        if e not in out:
            out.append(e)
    return out


def find_emails(website: str, client: httpx.Client | None = None, max_pages: int = 5,
                robots=None) -> list[str]:
    """Look at the homepage and the usual contact/careers pages. Best hiring email first."""
    import time
    from urllib.parse import urlsplit
    from .scrapers.boards import Robots
    url = website_for("", website)
    if not url:
        return []
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
    dom = urlsplit(url).netloc
    own = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": UA})
    robots = robots or Robots()
    found: list[str] = []
    try:
        for i, path in enumerate(CONTACT_PATHS[:max_pages + 2]):
            page = base + path
            try:
                if not robots.allowed(page):
                    continue
                r = client.get(page)
            except Exception:
                continue
            if r.status_code < 400:
                for e in emails_on_page(r.text, dom):
                    if e not in found:
                        found.append(e)
            if any(_rank(e) == 0 for e in found):
                break
            if own and i:
                time.sleep(1.5)
    finally:
        if own:
            client.close()
    return sorted(found, key=_rank)


def looks_like_company(website: str, company: str, client: httpx.Client | None = None) -> bool:
    """Is this homepage really this company? Checks the name appears on the page or in the domain."""
    words = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower())
             if w not in {"pvt", "ltd", "private", "limited", "llc", "inc", "the", "co", "smc", "pakistan",
                          "digital", "solutions", "technologies", "tech", "group", "agency", "marketing"}]
    if not words:
        return False
    url = website_for("", website)
    if any(w in url.lower() for w in words if len(w) >= 4):
        return True
    try:
        own = client is None
        client = client or httpx.Client(timeout=12, follow_redirects=True, headers={"User-Agent": UA})
        r = client.get(url)
        if own:
            client.close()
        text = r.text.lower()[:200000] if r.status_code < 400 else ""
    except Exception:
        return False
    hits = sum(1 for w in words if w in text)
    return hits >= max(1, len(words) - 1)
