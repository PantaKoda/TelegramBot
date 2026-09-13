"""Strict job-date parsing.

The bot stores screenshots under a YYYY/MM/DD prefix in R2, so a mis-parsed
date silently files a batch where nobody will look for it. Only one format is
accepted, and it is year-first so it can never be read day-first by mistake.
"""

import re
from datetime import date

DATE_FMT = "YYYY-MM-DD"
DATE_EG = "2026-12-29"

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_job_date(arg: str | None) -> date | None:
    """Return a date for a strictly-formatted YYYY-MM-DD string, else None.

    Two layers on purpose: the regex enforces shape (four digits, hyphens,
    zero padding), the date() constructor enforces reality (2026-02-30 passes
    the regex but is not a day).
    """
    if not arg:
        return None
    m = _DATE_RE.fullmatch(arg.strip())
    if not m:
        return None
    try:
        return date(*map(int, m.groups()))
    except ValueError:
        return None


def prefix_for(d: date) -> str:
    """R2 key prefix for a job date, e.g. '2026/12/29/'."""
    return f"{d:%Y/%m/%d}/"