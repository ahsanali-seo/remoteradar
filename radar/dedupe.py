"""Fingerprints so a job seen on LinkedIn and again on Indeed is processed once."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db
from .models import RawJob

TRACKING = {
    "fbclid", "gclid", "igshid", "igsh", "mibextid", "trk", "trkinfo", "refid", "trackingid",
    "lipi", "midtoken", "midsig", "eid", "ref", "ref_src", "src", "source", "originalsubdomain",
    "position", "pagenum", "ebp", "recommendedflavor", "from", "si", "__cft__", "__tn__",
    "_rdr", "rdid", "share_url", "vjk", "advn", "tk", "jsa", "sjdu", "acatk", "pub",
}

_LI_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?]*-)?(\d+)", re.I)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    m = _LI_JOB.search(url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    parts = urlsplit(url if "://" in url else "https://" + url)
    q = parse_qsl(parts.query, keep_blank_values=False)
    # LinkedIn search URLs carry the job as currentJobId
    for k, v in q:
        if k.lower() == "currentjobid" and "linkedin" in parts.netloc:
            return f"https://www.linkedin.com/jobs/view/{v}"
    q = [(k, v) for k, v in q if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    host = parts.netloc.lower()
    if host.startswith(("m.", "mobile.", "web.")):
        host = "www." + host.split(".", 1)[1]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(q)), ""))


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:24]


def fingerprints(job: RawJob) -> list[str]:
    fps = []
    u = normalize_url(job.url)
    if u:
        fps.append("url:" + _h(u))
    company, title = _norm_text(job.company), _norm_text(job.title)
    if company and title:
        fps.append("ct:" + _h(f"{company}|{title}"))
    if job.is_social or job.manual or not (company and title):
        body = re.sub(r"https?://\S+|#\w+|@\w+", " ", job.text or "")
        head = _norm_text(body)[:200]
        if len(head) >= 40:
            fps.append("txt:" + _h(head))
    return fps


REPOST_DAYS = 21          # same company + same role after this many days = a new posting


def _fresh(created: str | None) -> bool:
    from datetime import timedelta
    t = db.parse_iso(created)
    return t is None or t > db.now() - timedelta(days=REPOST_DAYS)


def seen(fps: list[str]) -> bool:
    """URL: never processed twice. Company+role or post text: only within REPOST_DAYS."""
    if not fps:
        return False
    qs = ",".join("?" for _ in fps)
    with db.connect() as con:
        rows = con.execute(f"SELECT fp, created_at FROM fingerprints WHERE fp IN ({qs})", fps).fetchall()
    for fp, created in rows:
        if fp.startswith("url:") or _fresh(created):
            return True
    return False


def earlier_posting(fps: list[str]) -> dict | None:
    """The older job this repost matches (same company + role), if any."""
    keys = [f for f in fps if not f.startswith("url:")]
    if not keys:
        return None
    qs = ",".join("?" for _ in keys)
    with db.connect() as con:
        r = con.execute(f"SELECT job_id, created_at FROM fingerprints WHERE fp IN ({qs}) AND job_id IS NOT NULL "
                        f"ORDER BY created_at LIMIT 1", keys).fetchone()
    if not r:
        return None
    job = db.get_job(r[0])
    return job


def remember(fps: list[str], job_id: int | None) -> None:
    with db.connect() as con:
        for fp in fps:
            # REPLACE so a repost restarts the 21-day window from today
            con.execute("INSERT OR REPLACE INTO fingerprints(fp, job_id, created_at) VALUES (?,?,?)",
                        (fp, job_id, db.iso()))
