"""The run: scrape every source → free filter → dedupe → Haiku check → Sonnet email → Gmail draft."""
from __future__ import annotations

import fcntl
import json
import re
import traceback
from dataclasses import dataclass, field

from . import db
from .ai import Claude, invented_numbers, leftover_placeholders
from .config import settings
from .dedupe import fingerprints, remember, seen
from .enrich import fetch_site, website_for
from .filters import city_of, free_filter
from .guard import Blocked, Budget, BudgetExhausted
from .models import RawJob


def subject_for(role: str) -> str:
    role = (role or "SEO Specialist").strip()
    return f"Application for {role} - Ahsan Ali, Senior SEO Consultant (8 Years)"


def load_profile() -> tuple[str, str]:
    p = settings.profile_dir / "profile.md"
    t = settings.profile_dir / "template.md"
    if not p.exists() or not t.exists():
        raise RuntimeError("profile/profile.md and profile/template.md are required. "
                           "Copy the .example files and fill them in.")
    profile, template = p.read_text(), t.read_text()
    todo = [l.strip() for l in profile.splitlines() if "TODO" in l and not l.strip().startswith("<!--")]
    if todo:
        raise RuntimeError(f"profile/profile.md still has {len(todo)} TODO line(s), e.g. "
                           f"\"{todo[0][:60]}\". Fill them in or delete them; no drafts until then.")
    return strip_comments(profile), strip_comments(template)


def strip_comments(text: str) -> str:
    import re
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


@dataclass
class RunStats:
    scanned: int = 0
    new: int = 0
    duplicates: int = 0
    filtered: int = 0
    rejected: int = 0
    drafted: int = 0
    no_email: int = 0
    capped: int = 0
    per_source: dict = field(default_factory=dict)
    drafted_jobs: list = field(default_factory=list)
    apply_jobs: list = field(default_factory=list)


class Pipeline:
    def __init__(self, claude: Claude | None = None, gmail=None, site_fetcher=fetch_site):
        self.claude = claude
        self.gmail = gmail
        self.site_fetcher = site_fetcher
        self.stats = RunStats()
        self._profile: tuple[str, str] | None = None

    # ------- one job through the whole funnel -------
    def process(self, job: RawJob) -> str:
        """Returns the stage the job ended in."""
        s = self.stats
        s.scanned += 1
        src = s.per_source.setdefault(job.source, {"scanned": 0, "new": 0})
        src["scanned"] += 1

        intl = job.extra.get("region") == "international"
        if intl and job.source == "linkedin_ww" and city_of(job.location or ""):
            intl = False                                   # a Lahore/Islamabad company: Local jobs
            job.extra["region"] = "local"
        if intl:
            from .filters import intl_filter
            ok, reason = intl_filter(job, settings.max_age_days * 2)
        else:
            ok, reason = free_filter(job, settings.max_age_days)
            # A remote SEO job elsewhere in Pakistan (Faisalabad, Multan...): International list
            if not ok and reason.startswith("not Lahore") and re.search(r"(?i)\bpakistan\b", job.blob()):
                from .filters import intl_filter
                intl = True
                job.extra["region"] = "international"
                ok, reason = intl_filter(job, settings.max_age_days * 2)
        if ok and job.company and db.is_blocked_company(job.company):
            ok, reason = False, "company on your do-not-show list"
        fps = fingerprints(job)
        from .dedupe import earlier_posting
        if seen(fps):
            s.duplicates += 1
            db.bump("duplicates")
            return "duplicate"
        if not ok:
            # Remember filtered posts too, so they are never re-checked
            remember(fps, None)
            s.filtered += 1
            return "filtered"

        s.new += 1
        src["new"] += 1
        db.bump("new_jobs")
        job_id = db.insert_job({
            "source": job.source, "url": job.url, "title": job.title, "company": job.company,
            "city": city_of(job.blob()) or "", "location": job.location,
            "posted_at": db.iso(job.posted_at) if job.posted_at else None,
            "text": job.text[:20000], "author": job.author, "stage": "checking",
            "region": "international" if intl else "local",
            "company_website": job.extra.get("website", "") or None,
        })
        prev = earlier_posting(fps)
        remember(fps, job_id)
        if prev:
            note = f"Reposted: this company posted the same role on {str(prev.get('created_at', ''))[:10]}"
            if prev.get("sent_at"):
                note += f"; you applied on {str(prev['sent_at'])[:10]}"
            db.update_job(job_id, notes=note)

        try:
            return self._ai_stages(job, job_id)
        except Exception as e:  # one bad job never stops the run
            db.update_job(job_id, stage="error", stage_reason=f"{type(e).__name__}: {e}"[:300])
            db.log(job.source, f"Error on a job: {type(e).__name__}: {e}", "warn")
            return "error"

    def _ai_stages(self, job: RawJob, job_id: int) -> str:
        s = self.stats
        if self.claude is None:
            db.update_job(job_id, stage="no_ai", stage_reason="No API key")
            return "no_ai"
        region = job.extra.get("region", "local")
        db.set_agent(job.source, "thinking", "AI is reading the post")
        meta = {"Title": job.title, "Company": job.company, "Location": job.location,
                "Posted by": job.author, "Link": job.url}
        r = self.claude.check(job.text, meta, region=region)
        board_names = {"rozee.pk", "rozee", "indeed", "mustakbil", "brightspyre", "linkedin"}
        company0 = job.company if job.company and job.company.strip().lower() not in board_names else ""
        fields = {k: v for k, v in {
            "title": job.title or r.title, "company": company0 or r.company or (r.details or {}).get("company", ""),
            "city": r.city or city_of(job.blob()) or "", "contact_email": r.contact_email,
            "company_website": r.company_website or job.extra.get("website", ""),
            "hiring_name": r.hiring_name,
            "details": json.dumps(r.details) if r.details else None,
            "match_score": r.match_score or None, "match_reason": r.match_reason or None,
            "eligibility": ("worldwide" if region == "international" and r.eligibility == "unclear"
                            and __import__("radar.filters", fromlist=["WORLDWIDE_RE"]).WORLDWIDE_RE.search(job.text or "")
                            else r.eligibility) or None,
            "email_source": "post" if r.contact_email else None}.items()}
        db.update_job(job_id, **fields)
        title = fields["title"] or "SEO role"
        company = fields["company"] or "the company"
        if not r.match or (region == "international" and r.eligibility == "restricted"):
            s.rejected += 1
            db.update_job(job_id, stage="rejected_ai", stage_reason=r.reason)
            return "rejected_ai"
        if fields["company"] and db.is_blocked_company(fields["company"]):
            db.update_job(job_id, stage="rejected_ai", stage_reason="Company on your do-not-show list")
            return "rejected_ai"

        from .filters import budget_flag, phones_in
        flag = budget_flag(r.details, title, region, settings.min_salary_pkr)
        if flag:
            r.match_score = max(0, (r.match_score or 0) - 25)
            db.update_job(job_id, budget_flag=flag, match_score=r.match_score or None)

        if not r.contact_email and not job.extra.get("ats"):
            self._find_email(job, job_id, company, r)

        if region == "local" or re.search(r"(?i)\bpakistan\b", job.blob()):
            phones = phones_in(job.text)
            if phones:
                db.update_job(job_id, phone=phones[0], phone_source="post")
            elif company != "the company":
                try:
                    ph = self.claude.find_phone(company, fields.get("city") or "Pakistan")
                except Exception:
                    ph = ""
                if ph:
                    db.update_job(job_id, phone=ph, phone_source="search")

        if not flag and (r.match_score or 0) >= settings.notify_score:
            from .followups import notify
            notify(f"Strong match ({r.match_score})", f"{title} at {company}")

        if not r.contact_email:
            s.no_email += 1
            why = ("Apply on site (this company only takes applications through its own form)"
                   if job.extra.get("ats") else "Apply on site")
            db.update_job(job_id, stage="no_email", stage_reason=why)
            s.apply_jobs.append({"title": title, "company": company, "url": job.url})
            db.log(job.source, f"New job without email: {title} at {company} (apply on site)")
            return "no_email"

        if flag:
            db.update_job(job_id, stage="low_budget",
                          stage_reason="Low budget / junior role: open it and press Apply by email if you still want it")
            db.log(job.source, f"{company}: junior or low-budget role, no automatic draft")
            return "low_budget"

        if db.recently_applied(company):
            db.update_job(job_id, stage="recent", stage_reason="You applied to this company in the last 30 days")
            db.log(job.source, f"{company}: applied in the last 30 days, no new draft")
            return "recent"

        if db.counter("drafts") >= settings.daily_draft_cap:
            s.capped += 1
            db.update_job(job_id, stage="capped", stage_reason=f"Daily cap of {settings.daily_draft_cap} reached; press Apply by email")
            s.apply_jobs.append({"title": title, "company": company, "url": job.url,
                                 "note": "daily draft cap reached, email: " + r.contact_email})
            return "capped"

        return self._write_and_draft(job, job_id, title, company, r)

    def _find_email(self, job, job_id, company, r) -> None:
        """No email in the post: look on the company's website, finding the website by search if needed."""
        from .enrich import find_emails, looks_like_company
        site = r.company_website or job.extra.get("website", "")
        source = "website"
        if not site and company and company != "the company":
            try:
                site = self.claude.find_website(company, job.location or "Pakistan")
            except Exception:
                site = ""
            if site and not looks_like_company(site, company):
                site = ""
            source = "search"
        if not site:
            return
        try:
            emails = find_emails(site)
        except Exception:
            emails = []
        db.update_job(job_id, company_website=site, found_emails=json.dumps(emails))
        if emails:
            r.contact_email = emails[0]
            r.company_website = site
            db.update_job(job_id, contact_email=emails[0], email_source=source)
            db.log(job.source, f"{company}: email found on their website ({emails[0]})")

    def _write_and_draft(self, job, job_id, title, company, r) -> str:
        s = self.stats
        if self._profile is None:
            try:
                self._profile = load_profile()
            except RuntimeError as e:
                self._profile = e
                db.log(None, str(e), "error")
        if isinstance(self._profile, Exception):
            db.update_job(job_id, stage="error", stage_reason="Profile not ready: " + str(self._profile)[:200])
            s.apply_jobs.append({"title": title, "company": company, "url": job.url,
                                 "note": "no draft, profile.md not ready; email: " + r.contact_email})
            return "error"
        profile, template = self._profile
        site_url = website_for(r.contact_email, r.company_website or job.extra.get("website", ""))
        site = self.site_fetcher(site_url) if site_url else ""

        db.set_agent(job.source, "thinking", f"Writing email for {company}")
        info = {"title": title, "company": company, "hiring_name": r.hiring_name,
                "where": job.extra.get("where", job.source), "text": job.text}
        body = ""
        for attempt in range(2):
            body = self.claude.write(profile, template, info, site)
            bad_nums = invented_numbers(body, profile, template, job.blob(), site)
            holes = leftover_placeholders(body)
            if body and not bad_nums and not holes:
                break
            problem = (f"unverified numbers {bad_nums}" if bad_nums else f"unfilled {holes}")
            body = ""
        if not body:
            db.update_job(job_id, stage="error", stage_reason=f"Email failed checks ({problem})")
            db.log(job.source, f"Skipped draft for {company}: email failed checks ({problem})", "warn")
            return "error"

        subject = subject_for(title)
        d = self.gmail.create_draft(r.contact_email, subject, body, attachment=settings.cv_path)
        db.bump("drafts")
        db.update_job(job_id, stage="drafted", status="Drafted", subject=subject,
                      draft_id=d["draft_id"], thread_id=d["thread_id"],
                      message_id_header=d.get("message_id_header"))
        s.drafted += 1
        s.drafted_jobs.append({"title": title, "company": company, "email": r.contact_email,
                               "url": job.url})
        db.log(job.source, f"Draft created for {company} ({title})")
        return "drafted"


# ---------------- running every source ----------------

def build_scrapers(claude=None):
    from .scrapers.boards import BoardsScraper, CareersScraper
    from .scrapers.facebook import FacebookScraper
    from .scrapers.instagram import InstagramScraper
    from .scrapers.linkedin import LinkedInGuestScraper, LinkedInScraper
    out = [LinkedInScraper() if settings.linkedin_login else LinkedInGuestScraper()]
    if settings.social_login:
        out += [FacebookScraper(), InstagramScraper(claude)]
    out += [BoardsScraper("indeed"), BoardsScraper("rozee"), CareersScraper()]
    if settings.international:
        from .scrapers.international import InternationalScraper
        out.append(InternationalScraper())
    return out


def no_login_off() -> list[str]:
    """Platforms switched off because no-login mode is on."""
    return [] if settings.social_login else ["facebook", "instagram"]


def scrape_one(scraper) -> list[RawJob]:
    from .browser import Session, open_browser
    p = scraper.platform
    log = lambda m: db.log(p, m)
    budget = Budget()
    if scraper.browser_profile is None:
        return scraper.run(None, log)
    with open_browser(scraper.browser_profile) as ctx:
        session = Session(p, ctx, budget)
        try:
            return scraper.run(session, log)
        except BudgetExhausted:
            log(f"Page-view limit ({budget.limit}) reached, stopping for this run")
            return getattr(scraper, "partial", [])


class RunLock:
    def __init__(self):
        self.path = settings.data_dir / "run.lock"
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A run is already in progress")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def make_clients():
    claude = gmail = None
    try:
        from .ai import make_ai
        claude = make_ai()
        if claude is None:
            db.log(None, "No AI key in .env (GEMINI_API_KEY or ANTHROPIC_API_KEY): "
                         "jobs are collected but not checked", "warn")
    except Exception as e:
        db.log(None, f"AI client failed: {e}", "warn")
    try:
        from .gmail import Gmail
        gmail = Gmail()
    except Exception as e:
        db.log("gmail", f"Gmail not connected: {e}", "warn")
    return claude, gmail


def run(only: list[str] | None = None) -> RunStats:
    db.init()
    with RunLock():
        claude, gmail = make_clients()
        if gmail is None and claude is not None:
            db.log(None, "Without Gmail, jobs are checked but no drafts are made", "warn")
        pipe = Pipeline(claude, gmail or _NoGmail())
        with db.connect() as con:
            run_id = con.execute("INSERT INTO runs(started_at) VALUES (?)", (db.iso(),)).lastrowid
        db.log(None, "Run started")

        try:
            db.backup()
        except Exception as e:
            db.log(None, f"Backup skipped: {e}", "warn")

        # Follow-ups and sent detection first (cheap, no scraping)
        if gmail is not None:
            from .followups import check_followups
            try:
                check_followups(gmail)
            except Exception as e:
                db.log("gmail", f"Follow-up check failed: {e}", "warn")

        sources = build_scrapers(claude)
        for p in no_login_off():
            db.set_agent(p, "paused", "Off: no-login mode (paste posts into Quick-add)")
        if gmail is not None:
            sources.append(_GmailAlerts(gmail))
        for sc in sources:
            if only and sc.platform not in only:
                continue
            if sc.platform in settings.skip_sources:
                db.set_agent(sc.platform, "paused", "Off: its jobs come from your Gmail job alerts")
                continue
            ok, why = db.platform_available(sc.platform)
            if not ok:
                db.log(sc.platform, f"Skipped: {why}")
                if "paused" in why:
                    db.set_agent(sc.platform, "paused", why)
                continue
            db.set_agent(sc.platform, "scraping", "Reading pages")
            before_new = pipe.stats.new
            before_scanned = pipe.stats.scanned
            try:
                found = scrape_one(sc)
                for j in found:
                    pipe.process(j)
                n_scan = pipe.stats.scanned - before_scanned
                n_new = pipe.stats.new - before_new
                db.log(sc.platform, f"{sc.name}: {n_scan} posts scanned, {n_new} new jobs")
                db.set_agent(sc.platform, "done", f"{n_scan} scanned · {n_new} new",
                             health="OK", last_success=db.iso())
            except Blocked:
                pass  # already logged, screenshotted, paused for 24h
            except Exception as e:
                db.set_agent(sc.platform, "error", f"{type(e).__name__}: {str(e)[:120]}", health="Warning")
                db.log(sc.platform, f"{sc.name} failed: {type(e).__name__}: {e}", "warn")
                traceback.print_exc()

        # Daily summary to myself
        if gmail is not None and settings.summary_draft:
            from .summary import send_summary_draft
            try:
                send_summary_draft(gmail, pipe.stats)
            except Exception as e:
                db.log("gmail", f"Summary draft failed: {e}", "warn")

        st = pipe.stats
        msg = (f"Run finished: {st.scanned} scanned, {st.new} new, {st.duplicates} duplicates, "
               f"{st.drafted} drafts, {st.no_email} apply-on-site")
        db.log(None, msg)
        with db.connect() as con:
            con.execute("UPDATE runs SET finished_at=?, summary=? WHERE id=?", (db.iso(), msg, run_id))
        return st


def process_quickadd(text: str) -> str:
    """Pasted WhatsApp message or link from the dashboard."""
    import re
    db.init()
    db.set_agent("quickadd", "scraping", "Reading what you pasted")
    url = (re.search(r"https?://\S+", text) or [""])[0]
    body = text
    if url and len(text.strip()) <= len(url) + 20:
        from .enrich import UA
        import httpx
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
            from .scrapers.boards import parse_detail
            body = parse_detail(r.text, url, "quickadd", {}, "a shared link").text or text
        except Exception:
            pass
    job = RawJob(source="quickadd", url=url, text=body, manual=True, is_social=True,
                 extra={"where": "a WhatsApp job group" if not url else "a shared job link"})
    claude, gmail = make_clients()
    pipe = Pipeline(claude, gmail or _NoGmail())
    stage = pipe.process(job)
    labels = {"duplicate": "Already seen", "filtered": "Not an SEO job",
              "rejected_ai": "Not a match", "no_email": "Added (no email, apply on site)",
              "drafted": "Draft created", "capped": "Daily cap reached", "error": "Error"}
    db.set_agent("quickadd", "done", labels.get(stage, stage), last_success=db.iso())
    db.log("quickadd", f"Quick-add: {labels.get(stage, stage)}")
    return stage


class _NoGmail:
    def create_draft(self, *a, **k):
        raise RuntimeError("Gmail is not connected (run `radar gmail-auth`)")


class _GmailAlerts:
    platform = "gmail"
    name = "Gmail alerts"
    browser_profile = None

    def __init__(self, gmail):
        self.gmail = gmail

    def run(self, session, log):
        jobs = self.gmail.alert_jobs()
        log(f'Label "{settings.alerts_label}": {len(jobs)} job links')
        return jobs



# ---------------- "Apply by email" from the dashboard ----------------

class _Contact:
    def __init__(self, email, website, hiring_name):
        self.contact_email, self.company_website, self.hiring_name = email, website, hiring_name


def draft_for_job(job_id: int, claude=None, gmail=None) -> dict:
    """Write ONE personalized email for a saved job and leave it as a Gmail draft.
    Used when you press "Apply by email" on a job. Never sends."""
    job = db.get_job(job_id)
    if not job:
        raise ValueError("No such job")
    if job.get("draft_id"):
        return {"draft_id": job["draft_id"], "existing": True}
    email = (job.get("contact_email") or "").strip()
    if not email:
        raise ValueError("This job has no email yet. Find one or type it in first.")
    if claude is None or gmail is None:
        c, g = make_clients()
        claude, gmail = claude or c, gmail or g
    if claude is None:
        raise RuntimeError("No AI key in .env")
    if gmail is None:
        raise RuntimeError("Gmail is not connected")
    raw = RawJob(source=job["source"], url=job.get("url") or "", title=job.get("title") or "",
                 company=job.get("company") or "", location=job.get("location") or "",
                 text=job.get("text") or "", author=job.get("author") or "",
                 extra={"where": _where(job), "website": job.get("company_website") or ""})
    pipe = Pipeline(claude, gmail)
    stage = pipe._write_and_draft(raw, job_id, job.get("title") or "SEO role", job.get("company") or "the company",
                                  _Contact(email, job.get("company_website") or "", job.get("hiring_name") or ""))
    after = db.get_job(job_id)
    if stage != "drafted":
        raise RuntimeError(after.get("stage_reason") or "Could not write the email")
    return {"draft_id": after["draft_id"], "existing": False}


def _where(job: dict) -> str:
    return {"linkedin": "LinkedIn Jobs", "indeed": "Indeed", "rozee": "Rozee.pk", "careers": "your careers page",
            "gmail": "a job alert", "quickadd": "a WhatsApp job group", "facebook": "a Facebook group post",
            "instagram": "your Instagram post"}.get(job.get("source"), job.get("source") or "your job post")
