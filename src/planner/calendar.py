"""ISO-week calendar helpers. Weeks are strings ``2026Wnn``; week 1 of 2026 starts 2025-12-29."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

YEAR = 2026
WEEK_RE = re.compile(r"^(\d{4})W(\d{2})$")


def week_label(n: int, year: int = YEAR) -> str:
    return f"{year}W{int(n):02d}"


def parse_week(label: str) -> tuple[int, int]:
    m = WEEK_RE.match(label)
    if not m:
        raise ValueError(f"bad week label {label!r}")
    return int(m.group(1)), int(m.group(2))


def week_num(label: str) -> int:
    """Week number within the planning year (2026W07 -> 7)."""
    return parse_week(label)[1]


def week_start(n: int, year: int = YEAR) -> date:
    """Monday of ISO week ``n``. ``week_start(1) == date(2025, 12, 29)``."""
    return date.fromisocalendar(year, int(n), 1)


def week_start_of(label: str) -> date:
    y, n = parse_week(label)
    return week_start(n, y)


def weeks_in_year(year: int = YEAR) -> int:
    return date(year, 12, 28).isocalendar()[1]


def week_labels(n_weeks: int = 52, year: int = YEAR) -> list[str]:
    return [week_label(i, year) for i in range(1, n_weeks + 1)]


def week_offset(d: date, year: int = YEAR) -> int:
    """Planning-week index of a date relative to the planning year.

    Returns 1 for dates in 2026W01, 0 for the week before (2025W52), negative further back,
    and > 52 for dates after the planning year. Works across year boundaries.
    """
    if isinstance(d, datetime):
        d = d.date()
    return (d - week_start(1, year)).days // 7 + 1


def date_of_offset(offset: int, year: int = YEAR) -> date:
    """Monday of planning-week index ``offset`` (inverse of ``week_offset``)."""
    return week_start(1, year) + timedelta(weeks=int(offset) - 1)


def lead_weeks(production_days: int, transit_days: int) -> int:
    """total_lead_weeks = ceil((production_days + transit_days) / 7)."""
    return math.ceil((int(production_days) + int(transit_days)) / 7)


def offset_label(offset: int, year: int = YEAR) -> str:
    """ISO week label of planning-week index ``offset`` (0 -> '2025W52', 1 -> '2026W01')."""
    iso = date_of_offset(offset, year).isocalendar()
    return f"{iso[0]}W{iso[1]:02d}"


def label_offset(label: str, year: int = YEAR) -> int:
    """Planning-week index of any ISO week label (inverse of ``offset_label``)."""
    y, n = parse_week(label)
    return week_offset(date.fromisocalendar(y, n, 1), year)
