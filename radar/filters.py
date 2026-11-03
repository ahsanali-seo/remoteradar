"""Free checks that run before any AI call. Pure Python, no network."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import RawJob

SEO_RE = re.compile(
    r"\bseo\b|search engine optimi[sz]ation|\blocal seo\b|\btechnical seo\b|"
    r"\bseo (executive|specialist|manager|consultant|analyst|lead|strategist|expert)\b",
    re.I,
)
CITY_RE = re.compile(r"\b(lahore|islamabad|karachi|rawalpindi)\b", re.I)
LOCAL_CITIES = ("Lahore", "Islamabad", "Karachi", "Rawalpindi")
REMOTE_RE = re.compile(
    r"\bremote(ly)?\b|\bwfh\b|work[\s-]from[\s-]home|\bhybrid\b|hybrid[\s-]remote|"
    r"\bfrom home\b|\banywhere\b|\bonline job\b",
    re.I,
)
# Phrases that mean "not remote" even if the word remote appears ("no remote")
NOT_REMOTE_RE = re.compile(r"\b(no|not) remote\b|\bon[\s-]?site only\b|\bremote not\b", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def city_of(text: str) -> str | None:
    m = CITY_RE.search(text or "")
    return m.group(1).capitalize() if m else None


def is_seo(text: str) -> bool:
    return bool(SEO_RE.search(text or ""))


def is_remote(text: str, hint: bool = False) -> bool:
    if NOT_REMOTE_RE.search(text or ""):
        return False
    return hint or bool(REMOTE_RE.search(text or ""))


def is_fresh(posted_at: datetime | None, max_age_days: int, now: datetime | None = None) -> bool:
    if posted_at is None:
        # Unknown date: social feeds are read newest-first and only the first screens,
        # so we keep it and let dedupe stop it from ever repeating.
        return True
    now = now or datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return now - posted_at <= timedelta(days=max_age_days)


def free_filter(job: RawJob, max_age_days: int = 7) -> tuple[bool, str]:
    """Returns (passes, reason). Quick-add items only need to look like SEO."""
    blob = job.blob()
    if not is_seo(blob):
        return False, "not an SEO role"
    if job.manual:
        return True, "manual"
    if not city_of(blob):
        return False, "not Lahore, Islamabad, Karachi or Rawalpindi"
    if not is_remote(blob, job.remote_hint):
        return False, "not remote"
    if not is_fresh(job.posted_at, max_age_days):
        return False, f"older than {max_age_days} days"
    return True, "ok"


def emails_in(text: str) -> list[str]:
    seen, out = set(), []
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if e.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) or e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


_REL_RE = re.compile(
    r"(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks|mo|month|months)\b(\s*ago)?",
    re.I,
)


def parse_relative(text: str, now: datetime | None = None) -> datetime | None:
    """'3d', '2 weeks ago', '5h', 'Just now', 'Yesterday' -> datetime (UTC)."""
    now = now or datetime.now(timezone.utc)
    t = (text or "").strip().lower()
    if not t:
        return None
    if "just now" in t or "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = _REL_RE.search(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("mo"):
        return now - timedelta(days=30 * n)
    if unit.startswith("m"):
        return now - timedelta(minutes=n)
    if unit.startswith("h"):
        return now - timedelta(hours=n)
    if unit.startswith("d"):
        return now - timedelta(days=n)
    if unit.startswith("w"):
        return now - timedelta(weeks=n)
    return None


# ---------------- international remote jobs ----------------
_OPEN = re.compile(r"\b(worldwide|anywhere|global(ly)?|all countries|any country|remote[- ]first|"
                   r"asia|apac|asia[- ]pacific|south asia|pakistan|middle east|mena|uae|dubai|gmt\+5|pkt)\b", re.I)
_LOCKED = re.compile(r"\b(us|usa|u\.s\.|united states|canada|uk|united kingdom|europe|eu|emea|latam|"
                     r"latin america|north america|americas|australia|new zealand|germany|france|spain|"
                     r"netherlands|poland|brazil|mexico|india)\b(\s*(only|based|residents?))?", re.I)


_COUNTRIES = re.compile(r"(?i)\b(united states|usa|u\.s\.a?|us|america|canada|mexico|brazil|argentina|chile|colombia|peru|"
    r"united kingdom|uk|england|scotland|ireland|germany|france|spain|portugal|italy|netherlands|belgium|"
    r"switzerland|austria|poland|czech(ia| republic)?|slovakia|hungary|romania|bulgaria|serbia|croatia|slovenia|"
    r"bosnia|greece|cyprus|malta|sweden|norway|denmark|finland|estonia|latvia|lithuania|ukraine|turkey|"
    r"israel|egypt|morocco|nigeria|kenya|south africa|ghana|india|bangladesh|sri lanka|nepal|philippines|"
    r"vietnam|thailand|malaysia|singapore|indonesia|japan|korea|china|hong kong|taiwan|australia|"
    r"new zealand|europe|eu|emea|latam|latin america|north america|americas|nordics|dach|cet|est|pst)\b")
_PK_OPEN = re.compile(r"(?i)\b(pakistan|asia|apac|asia[- ]pacific|south asia|middle east|mena|gmt\+5|pkt|"
                      r"worldwide|anywhere|global(ly)?|all countries|any country|any location)\b")


def eligibility(location: str) -> str:
    """'worldwide' | 'asia' | 'unclear' | 'restricted' from a job's location text."""
    loc = (location or "").strip()
    if not loc or re.fullmatch(r"(?i)\s*(remote|fully remote|100% remote|remote job)\s*", loc):
        return "unclear"
    if re.search(r"(?i)\b(worldwide|anywhere|global(ly)?|all countries|any country|any location)\b", loc):
        return "worldwide"
    if _PK_OPEN.search(loc):
        return "asia"
    if _COUNTRIES.search(loc) or "," in loc:
        return "restricted"          # names somewhere specific, and it isn't Pakistan
    return "unclear"


_PLACE = (r"(?:us|u\.s\.?|usa|u\.s\.a\.?|united states|america|american|uk|u\.k\.|united kingdom|british|"
          r"canada|canadian|australia|australian|eu|europe|european|germany|ireland|new zealand)")
RESIDENCY_RE = re.compile(
    r"(?i)(\b(?:must|need to|required to|should)\s+(?:be\s+)?(?:live|living|reside|residing|located|based)\s+in\s+(?:the\s+)?" + _PLACE + r"\b"
    r"|\b" + _PLACE + r"\s*(?:-|\s)?(?:citizens?|residents?|nationals?|only|based only)\b"
    r"|\b(?:remote|work from home|wfh)\s*(?:\(|-|–|,|in|within)\s*(?:the\s+)?" + _PLACE + r"\b(?!\s*(?:hours|time|business hours|tim(e|ing)s?|overlap))"
    r"|\b(?:authori[sz]ed|eligible|legally able|right)\s+to\s+work\s+in\s+(?:the\s+)?" + _PLACE + r"\b"
    r"|\bwork\s+authori[sz]ation\b|\bw-?2\b|\bsecurity clearance\b|\b(?:green card|us person)\b"
    r"|\bno\s+(?:visa\s+)?sponsorship\b)")
WORLDWIDE_RE = re.compile(
    r"(?i)\b(work from anywhere|hire (?:from )?anywhere|anywhere in the world|worldwide|world-wide|globally distributed|"
    r"all countries|any country|any location|location[- ]independent|international contractors?|"
    r"contractors? (?:from|in) any|we hire globally|open to (?:all|any) (?:countries|locations)|fully distributed)\b")


def residency_rule(text: str) -> str:
    """The phrase that limits the job to people living in some country, or ''."""
    m = RESIDENCY_RE.search(text or "")
    return m.group(0).strip() if m else ""


def intl_filter(job: RawJob, max_age_days: int = 14) -> tuple[bool, str]:
    """Cheap checks for international remote jobs before any AI call."""
    if not is_seo(job.title):        # title only: feeds tag lots of unrelated jobs "seo"
        return False, "not an SEO role"
    if eligibility(job.location) == "restricted":
        return False, f"location limited ({job.location[:40]})"
    rule = residency_rule(job.text)
    if rule and not WORLDWIDE_RE.search(job.text or ""):
        return False, f"only for people living there (\"{rule[:50]}\")"
    if not is_fresh(job.posted_at, max_age_days):
        return False, f"older than {max_age_days} days"
    return True, "ok"


# ---------------- Pakistani phone numbers (for WhatsApp) ----------------
_PHONE = re.compile(r"(?<!\d)(?:\+?92[\s\-.]?|0092[\s\-.]?|0)(3\d{2})[\s\-.]?(\d{3})[\s\-.]?(\d{4})(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)(?:\+?92[\s\-.]?|0)(42|51|21)[\s\-.]?(\d{3,4})[\s\-.]?(\d{4})(?!\d)")


def phones_in(text: str) -> list[str]:
    """Pakistani mobile numbers (the ones that can have WhatsApp) as 923001234567."""
    out = []
    for a, b, c in _PHONE.findall(text or ""):
        n = f"92{a}{b}{c}"
        if n not in out:
            out.append(n)
    return out


def landlines_in(text: str) -> list[str]:
    return [f"92{a}{b}{c}" for a, b, c in _LANDLINE.findall(text or "")]


# ---------------- budget / seniority ----------------
_SAL = re.compile(r"(?i)(?:pkr|rs\.?|rupees)?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*(k|lac|lakh|lacs)?")


def monthly_pkr(salary: str) -> int:
    """Rough monthly PKR from text like 'PKR 60,000 - 80,000', 'Rs 1.2 lac', '50k-70k'. 0 if unknown."""
    if not salary or re.search(r"(?i)usd|\$|eur|€|gbp|£|aed", salary):
        return 0
    vals = []
    for num, unit in _SAL.findall(salary):
        try:
            v = float(num.replace(",", "").replace(" ", ""))
        except ValueError:
            continue
        unit = unit.lower()
        if unit == "k":
            v *= 1000
        elif unit.startswith("la"):
            v *= 100000
        if v >= 5000:
            vals.append(v)
    if not vals:
        return 0
    top = max(vals)
    if re.search(r"(?i)year|annual|per annum|p\.a", salary):
        top /= 12
    return int(top)


def budget_flag(details: dict | None, title: str = "", region: str = "local", min_salary: int = 100000) -> str:
    """'low' when the post looks like a junior budget for an 8-year senior, else ''."""
    d = details or {}
    sen = str(d.get("seniority", "")).lower()
    try:
        years = int(float(d.get("min_years") or 0))
    except (TypeError, ValueError):
        years = 0
    if re.search(r"(?i)\b(intern|internship|trainee|fresh(er)?|junior|jr\.?)\b", title or ""):
        return "low"
    if sen in ("intern", "junior"):
        return "low"
    if region == "local":
        pay = monthly_pkr(str(d.get("salary", "")))
        if pay and pay < min_salary:
            return "low"
        if 0 < years <= 2 and not pay:
            return "low"
    return ""
