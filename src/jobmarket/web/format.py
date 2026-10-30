"""Display formatting for the website: times in America/Chicago, salary ranges, labels."""

from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Chicago")

STATE_LABELS = {
    "observed": "Observed",
    "possibly_unavailable": "Possibly unavailable",
    "assumed_inactive": "Assumed inactive",
    "closed": "Closed",
}
SOURCE_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever"}
FLAG_REASONS = {
    "title location description": "same title, location, and description",
    "title_location_description": "same title, location, and description",
    "requisition id conflict": "same requisition ID used for different titles",
}
DATE_KIND_LABELS = {"published": "Published", "created": "Created"}
ARRANGEMENT_LABELS = {
    "onsite": "On site",
    "hybrid": "Hybrid",
    "remote": "Remote",
    "multiple": "Several arrangements",
    "not_stated": "Not stated",
    "ambiguous": "Unclear",
}
EMPLOYMENT_LABELS = {
    "full_time": "Full-time",
    "part_time": "Part-time",
    "contract": "Contract",
    "contract_to_hire": "Contract to hire",
    "temporary": "Temporary",
    "internship": "Internship",
    "apprenticeship": "Apprenticeship",
    "not_stated": "Not stated",
    "ambiguous": "Unclear",
}
_INTERVALS = {
    "per-year-salary": "year",
    "per-month-salary": "month",
    "per-week-salary": "week",
    "per-day-wage": "day",
    "per-hour-wage": "hour",
    "one-time": "one time",
}


def local(value: datetime | None) -> datetime | None:
    return value.astimezone(ZONE) if value else None


def when(value: datetime | None) -> str:
    """'Oct 28, 2026, 7:15 PM CDT'."""
    if value is None:
        return "unknown"
    t = local(value)
    return f"{t:%b} {t.day}, {t.year}, {t.hour % 12 or 12}:{t:%M} {t:%p} {t:%Z}"


def day(value: datetime | None) -> str:
    """'Oct 28, 2026'."""
    if value is None:
        return "unknown"
    t = local(value)
    return f"{t:%b} {t.day}, {t.year}"


def day_only(value) -> str:
    """A calendar date ('Oct 28, 2026')."""
    return f"{value:%b} {value.day}, {value.year}" if value else "unknown"


def relative_day(value: datetime | None, now: datetime) -> str:
    """'today', 'yesterday', or the date, by America/Chicago calendar days."""
    if value is None:
        return "unknown"
    days = (local(now).date() - local(value).date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"on {day(value)}"


def age(value: datetime | None, now: datetime) -> str:
    """'3 hours ago', '2 days ago'."""
    if value is None:
        return "never"
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 3600:
        minutes = seconds // 60
        return "just now" if minutes < 1 else f"{minutes} minute{'s' * (minutes != 1)} ago"
    if seconds < 48 * 3600:
        hours = seconds // 3600
        return f"{hours} hour{'s' * (hours != 1)} ago"
    days = seconds // 86400
    return f"{days} days ago"


def salary(value: dict | None) -> str | None:
    """A source-supplied salary range ('$150,000-$220,000 / year'), or None."""
    if not value:
        return None
    low, high = value.get("min"), value.get("max")
    if not isinstance(low, int | float) and not isinstance(high, int | float):
        return None
    currency = value.get("currency") or ""
    unit = _INTERVALS.get(
        value.get("interval") or "", (value.get("interval") or "").replace("-", " ")
    )

    def amount(n):
        text = f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"
        return f"${text}" if currency == "USD" else text

    if isinstance(low, int | float) and isinstance(high, int | float) and low != high:
        span = f"{amount(low)}–{amount(high)}"
    else:
        span = amount(low if isinstance(low, int | float) else high)
    prefix = "" if currency in ("USD", "") else f"{currency} "
    return f"{prefix}{span}{f' / {unit}' if unit else ''}"


def words(value: str | None) -> str:
    return (value or "").replace("_", " ")


def join_natural(items: list[str], word: str = "and") -> str:
    """'Python', 'Python and SQL', 'Python, SQL, and Airflow'."""
    if len(items) <= 2:
        return f" {word} ".join(items)
    return f"{', '.join(items[:-1])}, {word} {items[-1]}"
