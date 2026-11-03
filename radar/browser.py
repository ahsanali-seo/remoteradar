"""Chrome (with a window on the Mac, headless in the cloud) with one saved profile per platform. Read-only browsing helpers."""
from __future__ import annotations

import random
import time
from contextlib import contextmanager
from pathlib import Path

from . import db
from .config import settings
from .guard import BLOCK_SELECTORS, Blocked, Budget, detect_block, human_pause

VIEWPORT = {"width": 1440, "height": 860}


@contextmanager
def open_browser(platform: str):
    from playwright.sync_api import sync_playwright

    profile = settings.browser_dir / platform
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        kwargs = dict(
            user_data_dir=str(profile), headless=settings.headless, viewport=VIEWPORT,
            locale="en-US", timezone_id="Asia/Karachi",
        )
        ctx = None
        if settings.use_system_chrome:
            try:
                ctx = pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
            except Exception:
                ctx = None
        if ctx is None:
            ctx = pw.chromium.launch_persistent_context(**kwargs)
        try:
            yield ctx
        finally:
            ctx.close()


class Session:
    """Wraps one page with the anti-block rules applied to every navigation."""

    def __init__(self, platform: str, ctx, budget: Budget | None = None):
        self.platform = platform
        self.ctx = ctx
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.budget = budget or Budget()

    def check(self) -> None:
        p = self.page
        try:
            text = p.inner_text("body", timeout=5000)
        except Exception:
            text = ""
        has_el = False
        for sel in BLOCK_SELECTORS:
            try:
                if p.locator(sel).first.is_visible(timeout=300):
                    has_el = True
                    break
            except Exception:
                pass
        reason = detect_block(p.url, text, has_el)
        if reason:
            shot = self.screenshot("blocked")
            db.block_platform(self.platform, reason, shot)
            raise Blocked(reason)

    def goto(self, url: str) -> None:
        self.budget.spend()
        human_pause()
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(2.0, 4.0))
        self.check()

    def scroll(self, times: int = 4) -> None:
        """Natural scrolling: uneven steps, small pauses, occasional scroll back."""
        for i in range(times):
            for _ in range(random.randint(3, 6)):
                self.page.mouse.wheel(0, random.randint(180, 420))
                time.sleep(random.uniform(0.25, 0.8))
            if random.random() < 0.2:
                self.page.mouse.wheel(0, -random.randint(100, 250))
            time.sleep(random.uniform(1.5, 3.5))
            self.check()

    def html(self) -> str:
        return self.page.content()

    def screenshot(self, name: str) -> str | None:
        try:
            settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path: Path = settings.screenshots_dir / f"{self.platform}-{name}-{int(time.time())}.png"
            self.page.screenshot(path=str(path))
            return path.name
        except Exception:
            return None
