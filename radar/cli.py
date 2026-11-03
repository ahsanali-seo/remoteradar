"""radar run | radar login <platform> | radar status | radar serve | radar gmail-auth | radar schedule"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
import time
from pathlib import Path

from . import db
from .config import LOGIN_PLATFORMS, PLATFORMS, settings

PLIST_LABELS = ["com.ahsan.remoteradar.run", "com.ahsan.remoteradar.dashboard",
                "com.ahsan.remoteradar.watch", "com.ahsan.remoteradar.catchup"]
LAST_FULL = "last_full_run.txt"


def cmd_run(a):
    if a.jitter:
        wait = random.randint(0, a.jitter * 60)
        print(f"Waiting {wait // 60} min {wait % 60} s before starting (randomized start)…", flush=True)
        db.init()
        db.log(None, f"Scheduled run starts in {wait // 60} min")
        time.sleep(wait)
    from .pipeline import run
    only = a.only.split(",") if a.only else None
    if not only:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / LAST_FULL).write_text(str(time.time()))
    try:
        st = run(only)
    except RuntimeError as e:
        print(e)
        sys.exit(1)
    print(f"\nDone: {st.new} new jobs, {st.drafted} drafts, {st.no_email} apply-on-site.")


def cmd_login(a):
    p = a.platform.lower()
    if p not in LOGIN_PLATFORMS:
        sys.exit(f"Choose one of: {', '.join(LOGIN_PLATFORMS)}")
    from .browser import open_browser
    print(f"Opening {p} in RemoteRadar's own browser.\n"
          f"Log in with your SPARE {p} account by hand. Clear any checks it shows.\n"
          f"When you see your feed, come back here and press Enter.")
    with open_browser(p) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_PLATFORMS[p])
        input("\nPress Enter after you are logged in… ")
    db.init()
    a_ = db.get_agent(p)
    if a_.get("status") == "blocked":
        db.clear_block(p)
    db.set_agent(p, "idle", "Logged in", health="OK")
    db.log(p, "Session saved on this Mac")
    print("Saved. No password is stored; only the browser session in browser_profiles/.")


def cmd_status(a):
    db.init()
    print(f"{'Source':16} {'Health':8} {'Status':9} {'Enabled':7} Last success")
    for ag in db.agents():
        print(f"{ag['label']:16} {ag['health']:8} {ag['status']:9} {'yes' if ag['enabled'] else 'no':7} "
              f"{(ag['last_success'] or '-')[:16]}")
    from .followups import due_count
    print(f"\nToday: {db.counter('new_jobs')} new jobs · {db.counter('duplicates')} duplicates · "
          f"{db.counter('drafts')}/{settings.daily_draft_cap} drafts · {due_count()} follow-ups due · "
          f"AI cost ${db.ai_cost_today():.2f}")


def cmd_serve(a):
    import uvicorn
    if settings.cloud:
        from . import cloud
        cloud.prepare()          # bring back the database and profile from the private dataset
    db.init()
    print(f"Dashboard: http://{'localhost' if settings.host == '127.0.0.1' else settings.host}:{settings.port}",
          flush=True)
    uvicorn.run("radar.server:app", host=settings.host, port=settings.port, log_level="warning")


def cmd_gmail_auth(a):
    from .gmail import Gmail, authorize, scopes
    authorize(interactive=True)
    g = Gmail()
    lid = g.label_id()
    print("Gmail connected with scopes:\n  " + "\n  ".join(scopes()))
    if lid:
        print(f'Label "{settings.gmail_label}" is ready.')


def _plist(label: str, args: list[str], calendar: list[tuple[int, int]] | None, keepalive: bool,
           interval: int = 0, at_load: bool = False) -> str:
    py = sys.executable
    arr = "".join(f"\n    <string>{x}</string>" for x in [py, "-m", "radar.cli", *args])
    cal = ""
    if calendar:
        items = "".join(f"\n    <dict><key>Hour</key><integer>{h}</integer><key>Minute</key>"
                        f"<integer>{m}</integer></dict>" for h, m in calendar)
        cal = f"\n  <key>StartCalendarInterval</key>\n  <array>{items}\n  </array>"
    ka = "\n  <key>KeepAlive</key><true/>\n  <key>RunAtLoad</key><true/>" if keepalive else ""
    if interval:
        ka += f"\n  <key>StartInterval</key><integer>{interval}</integer>"
    if at_load and not keepalive:
        ka += "\n  <key>RunAtLoad</key><true/>"
    logs = settings.data_dir / f"{label.split('.')[-1]}.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>{arr}
  </array>
  <key>WorkingDirectory</key><string>{settings.home}</string>
  <key>EnvironmentVariables</key>
  <dict><key>RADAR_HOME</key><string>{settings.home}</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>{cal}{ka}
  <key>StandardOutPath</key><string>{logs}</string>
  <key>StandardErrorPath</key><string>{logs}</string>
</dict>
</plist>
"""


def cmd_schedule(a):
    """Install (or remove) the launchd jobs: runs at 09:00 and 18:00 + random 0-40 min, and the dashboard."""
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    files = {
        # Full runs 4 times a day (Pakistan, UK and US working hours), random start within 20 min
        PLIST_LABELS[0]: _plist(PLIST_LABELS[0], ["run", "--jitter", "20"],
                                [(9, 0), (14, 0), (19, 0), (23, 0)], False),
        PLIST_LABELS[1]: _plist(PLIST_LABELS[1], ["serve"], None, True),
        # Light check every 30 min: public feeds, company boards and Gmail alerts only
        PLIST_LABELS[2]: _plist(PLIST_LABELS[2], ["run", "--only", "intl,gmail,careers"], None, False,
                                interval=1800),
        # When you log in / the Mac starts: full run if the last one was over 5 hours ago
        PLIST_LABELS[3]: _plist(PLIST_LABELS[3], ["catchup"], None, False, at_load=True),
    }
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    for label, xml in files.items():
        dest = agents / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(dest)], capture_output=True)
        if a.off:
            dest.unlink(missing_ok=True)
            print(f"Removed {label}")
            continue
        dest.write_text(xml)
        (settings.home / "launchd" / f"{label}.plist").write_text(xml)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(dest)], capture_output=True, text=True)
        print(f"{'Installed' if r.returncode == 0 else 'Could not load'} {label} {r.stderr.strip()}")
    if not a.off:
        print("Full runs: about 09:00, 14:00, 19:00 and 23:00 (Mac time; set it to Pakistan time).\n"
              "Light check of international sites, company boards and Gmail alerts every 30 minutes.\n"
              "When the Mac starts or wakes after a long time off, a catch-up run starts by itself.")


def cmd_catchup(a):
    """Full run only if the last full run is more than 5 hours old (Mac was off or asleep)."""
    f = settings.data_dir / LAST_FULL
    try:
        last = float(f.read_text())
    except Exception:
        last = 0
    if time.time() - last < 5 * 3600:
        print("Last full run was recent; nothing to catch up.")
        return
    time.sleep(90)                    # let Wi-Fi connect after login
    a.only, a.jitter = None, 0
    cmd_run(a)


def cmd_test_intl(a):
    """Show what each international source returns right now (no AI, nothing saved)."""
    from .filters import intl_filter
    from .scrapers.international import InternationalScraper
    db.init()
    jobs = InternationalScraper(pause=1.0).run(None, lambda m: print("  " + m))
    ok = [j for j in jobs if intl_filter(j, settings.max_age_days * 2)[0]]
    print(f"\n{len(jobs)} SEO jobs found, {len(ok)} pass the location/date filter:")
    for j in ok[:25]:
        print(f"  - {j.title} · {j.company} · {j.location} · {j.extra.get('where')}")


def cmd_set_password(a):
    """Set (or remove with --off) the dashboard password. Only a hash is stored."""
    import getpass
    from . import auth
    if a.off:
        auth.disable()
        print("Password removed. The dashboard opens without logging in.")
        return
    p1 = getpass.getpass("New dashboard password (typing is hidden): ")
    p2 = getpass.getpass("Type it again: ")
    if p1 != p2:
        sys.exit("The two passwords are different. Nothing changed.")
    try:
        auth.set_password(p1)
    except ValueError as e:
        sys.exit(str(e))
    print("Password set. Refresh http://localhost:8787 and log in. Other open sessions were logged out.")


def cmd_cloud_run(a):
    """One run in GitHub Actions: bring data back, run, save it, build the locked page."""
    from . import cloud, site
    cloud.prepare()
    if not cloud._status["restored"]:
        sys.exit("Could not read the private data (check the HF_TOKEN and DATA_REPO secrets). Nothing was run.")
    db.init()
    mode = a.mode
    if mode == "light" and time.time() - cloud._last_full() > 6 * 3600:
        print("Last full run is over 6 hours old: doing a full run instead", flush=True)
        mode = "full"
    only = None if mode == "full" else ["intl", "gmail", "careers"]
    if only is None:
        (settings.data_dir / LAST_FULL).write_text(str(time.time()))
    failed = None
    try:
        from .pipeline import run
        st = run(only)
        print(f"\nDone: {st.new} new jobs, {st.drafted} drafts, {st.no_email} apply-on-site.", flush=True)
    except Exception as e:                     # still save what was found and rebuild the page
        failed = f"{type(e).__name__}: {e}"
        print(f"Run problem: {failed}", flush=True)
    if not cloud.save(f"{mode} run"):
        if cloud._status.get("last_save_error"):
            sys.exit(f"Saving failed: {cloud._status['last_save_error']}")
    if a.site:
        page = site.build(Path(a.site))
        print(f"Page built: {page}", flush=True)
    if mode == "full" and time.localtime().tm_hour < 12:
        cloud.squash()                         # once a day, keep the private dataset small
    if failed:
        sys.exit(1)


def cmd_site(a):
    from . import site
    print(site.build(Path(a.out)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="radar", description="Ahsan RemoteRadar")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Scrape all sources and create drafts")
    r.add_argument("--jitter", type=int, default=0, help="wait a random 0..N minutes first")
    r.add_argument("--only", help=f"comma list of: {','.join(PLATFORMS)}")
    r.set_defaults(fn=cmd_run)
    l = sub.add_parser("login", help="Log in by hand once: linkedin | facebook | instagram")
    l.add_argument("platform")
    l.set_defaults(fn=cmd_login)
    sub.add_parser("status", help="Account health and today's numbers").set_defaults(fn=cmd_status)
    sub.add_parser("serve", help="Start the dashboard").set_defaults(fn=cmd_serve)
    sp = sub.add_parser("set-password", help="Set the dashboard password (or --off)")
    sp.add_argument("--off", action="store_true", help="remove the password")
    sp.set_defaults(fn=cmd_set_password)
    sub.add_parser("catchup", help="Full run if the last one is over 5 hours old").set_defaults(fn=cmd_catchup)
    sub.add_parser("test-intl", help="Check every international source now").set_defaults(fn=cmd_test_intl)
    sub.add_parser("gmail-auth", help="Connect Gmail (opens Google sign-in)").set_defaults(fn=cmd_gmail_auth)
    cr = sub.add_parser("cloud-run", help="(GitHub Actions) restore data, run, save, build page")
    cr.add_argument("--mode", choices=["full", "light"], default="full")
    cr.add_argument("--site", help="folder for the locked read-only page")
    cr.set_defaults(fn=cmd_cloud_run)
    st_ = sub.add_parser("site", help="Build the password-locked read-only page")
    st_.add_argument("--out", default="site")
    st_.set_defaults(fn=cmd_site)
    s = sub.add_parser("schedule", help="Install the twice-daily schedule and dashboard service")
    s.add_argument("--off", action="store_true", help="remove the schedule")
    s.set_defaults(fn=cmd_schedule)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
