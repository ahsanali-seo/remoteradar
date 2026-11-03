"""Anti-block rules: human pacing, page-view budget, and stop-on-warning detection."""
from __future__ import annotations

import random
import re
import time

from .config import settings


class Blocked(Exception):
    """A CAPTCHA, checkpoint, login wall or 'unusual activity' page appeared."""


class BudgetExhausted(Exception):
    """This platform used its page views for this run."""


BLOCK_URL_RE = re.compile(
    r"checkpoint|captcha|challenge|/authwall|/login|/uas/login|accounts/login|"
    r"/suspended|/restricted|two_step_verification|/sorry/|cdn-cgi/challenge",
    re.I,
)
BLOCK_TEXT_RE = re.compile(
    r"unusual activity|suspicious activity|verify (that )?you('| a)re (a )?(human|not a robot)|"
    r"security check|are you a robot|confirm (it'?s|that it'?s) you|we'?ve restricted|"
    r"your account (has been|is) (temporarily )?(restricted|locked|suspended)|"
    r"complete the captcha|press (and|&) hold|i'?m not a robot|help us confirm",
    re.I,
)
BLOCK_SELECTORS = [
    "iframe[src*='captcha']", "iframe[src*='recaptcha']", "iframe[title*='challenge' i]",
    "#captcha", "[id*='captcha' i]", "div.g-recaptcha", "#challenge-form", "#px-captcha",
]


def detect_block(url: str, text: str, has_block_element: bool = False) -> str | None:
    """Pure function so it can be tested with sample pages."""
    if BLOCK_URL_RE.search(url or ""):
        return f"redirected to a check/login page ({url[:80]})"
    m = BLOCK_TEXT_RE.search((text or "")[:20000])
    if m:
        return f'page says "{m.group(0)}"'
    if has_block_element:
        return "a CAPTCHA appeared"
    return None


def human_pause(lo: float | None = None, hi: float | None = None) -> None:
    time.sleep(random.uniform(lo or settings.min_delay, hi or settings.max_delay))


class Budget:
    def __init__(self, limit: int | None = None):
        self.limit = limit or settings.max_page_views
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise BudgetExhausted(f"{self.limit} page views used")
        self.used += 1

    @property
    def left(self) -> int:
        return self.limit - self.used
