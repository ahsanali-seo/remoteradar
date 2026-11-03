from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawJob:
    """One job candidate exactly as a scraper saw it. Text is untrusted data."""
    source: str                    # platform key, e.g. "linkedin"
    url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    text: str = ""                 # description / post text / caption
    author: str = ""
    posted_at: datetime | None = None
    remote_hint: bool = False      # e.g. scraped with the LinkedIn Remote filter
    is_social: bool = False        # LinkedIn/Facebook/Instagram posts
    manual: bool = False           # pasted into quick-add
    extra: dict = field(default_factory=dict)

    def blob(self) -> str:
        return "\n".join(x for x in [self.title, self.company, self.location, self.text] if x)


STATUSES = [
    "New", "Drafted", "Sent", "Follow-up 1", "Follow-up 2",
    "Replied", "Interview", "Rejected",
]
