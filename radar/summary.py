"""After each run: one draft to yourself listing drafted jobs and apply-on-site links."""
from __future__ import annotations

from datetime import datetime

from . import db
from .config import settings


def summary_text(stats) -> tuple[str, str]:
    date = datetime.now().strftime("%d %b %Y")
    subject = f"RemoteRadar - {date} - {stats.new} new jobs"
    lines = [f"RemoteRadar run at {datetime.now().strftime('%H:%M')}", "",
             f"Scanned {stats.scanned} posts · {stats.new} new · {stats.duplicates} duplicates skipped",
             f"AI cost today: ${db.ai_cost_today():.2f}", ""]
    lines.append(f"DRAFTS READY IN GMAIL ({len(stats.drafted_jobs)}) — label {settings.gmail_label}")
    for j in stats.drafted_jobs or []:
        lines.append(f"• {j['title']} at {j['company']} → {j['email']}\n  {j['url']}")
    if not stats.drafted_jobs:
        lines.append("• none this run")
    lines += ["", f"APPLY ON SITE ({len(stats.apply_jobs)})"]
    for j in stats.apply_jobs or []:
        note = f" ({j['note']})" if j.get("note") else ""
        lines.append(f"• {j['title']} at {j['company']}{note}\n  {j['url']}")
    if not stats.apply_jobs:
        lines.append("• none this run")
    import os
    host = os.environ.get("SPACE_HOST")
    lines += ["", f"Dashboard: https://{host}" if host else f"Dashboard: http://localhost:{settings.port}"]
    return subject, "\n".join(lines)


def send_summary_draft(gmail, stats) -> None:
    """Creates a DRAFT addressed to you. Nothing is sent."""
    if stats.new == 0 and not stats.drafted_jobs and not stats.apply_jobs:
        db.log("gmail", "No new jobs, so no summary draft this run")
        return
    subject, body = summary_text(stats)
    gmail.create_draft(settings.gmail_address, subject, body, label=False)
    db.log("gmail", f'Summary draft created: "{subject}"')
