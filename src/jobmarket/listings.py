"""Listing data for the job list and detail pages: the stored description in a safe display
form, the original links, and freshness and availability.

Descriptions come from employers' ATS pages and are untrusted HTML. They are never rendered
as collected. safe_description_html() rebuilds them from an allowlist: headings, paragraphs,
line breaks, lists, bold, italics, block quotes, and https links (opened in a new tab without
passing the referrer). Every attribute except a link's address is dropped, scripts, styles,
images, embedded frames, and forms are removed with their content, and all text is escaped.
The raw responses stay the evidence; this is only a display form.

The text shown is what the source API supplied (Greenhouse job content; Lever description,
lists, and additional text). It can differ from the employer's own page, so pages should say
where the text came from and link to the original.
"""

import html
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import psycopg

DISPLAY_RULES_VERSION = "2026-10-27.1"

_ALLOWED = {"p", "br", "ul", "ol", "li", "strong", "em", "h2", "h3", "h4", "blockquote", "a"}
_RENAMED = {"b": "strong", "i": "em", "h1": "h2", "h5": "h4", "h6": "h4"}
# Removed together with everything inside them.
_DROPPED_WITH_CONTENT = {
    "script", "style", "iframe", "object", "svg", "math", "noscript", "template",
    "head", "title", "form", "button", "select", "textarea", "canvas", "video", "audio",
}  # fmt: skip
# Not kept, but they separate blocks of text, so their end becomes a line break.
_BLOCK_BOUNDARIES = {
    "div", "section", "article", "header", "footer", "table", "tr", "dl", "dt", "dd", "pre",
    "hr", "main", "aside", "figure", "figcaption", "address",
}  # fmt: skip
_VOID = {"br"}
_BLOCK_ENDS = {"</p>", "</li>", "</ul>", "</ol>", "</h2>", "</h3>", "</h4>", "</blockquote>"}

SOURCE_NOTES = {
    "greenhouse": "Text as published through the employer's Greenhouse job board API.",
    "lever": "Text as published through the employer's Lever postings API (description, "
    "lists, and additional information).",
}


def safe_link(href: str | None) -> str | None:
    """The address if it is an absolute https URL, otherwise None."""
    if not href:
        return None
    href = href.strip()
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.netloc or any(c in href for c in "\r\n\t "):
        return None
    return href


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.dropping: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.dropping:
            if tag in _DROPPED_WITH_CONTENT:
                self.dropping.append(tag)
            return
        if tag in _DROPPED_WITH_CONTENT:
            self.dropping.append(tag)
            return
        tag = _RENAMED.get(tag, tag)
        if tag not in _ALLOWED:
            return
        if tag in _VOID:
            self.out.append("<br>")
            return
        if tag == "a":
            href = safe_link(dict(attrs).get("href"))
            if href is None:
                return  # keep the link text, drop the link
            self.out.append(
                f'<a href="{html.escape(href, quote=True)}" rel="nofollow noopener noreferrer" '
                'target="_blank">'
            )
        else:
            self.out.append(f"<{tag}>")
        self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if not self.dropping and _RENAMED.get(tag, tag) in _VOID:
            self.out.append("<br>")
        elif not self.dropping and tag in _BLOCK_BOUNDARIES:
            self._boundary()

    def handle_endtag(self, tag):
        if self.dropping:
            if tag == self.dropping[-1]:
                self.dropping.pop()
            return
        if tag in _BLOCK_BOUNDARIES:
            self._boundary()
            return
        tag = _RENAMED.get(tag, tag)
        if tag not in self.open:
            return  # an end tag with no open element (or a link that was dropped)
        while self.open:
            current = self.open.pop()
            self.out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(html.escape(data, quote=False))

    def _boundary(self):
        if self.out and self.out[-1] != "<br>" and self.out[-1] not in _BLOCK_ENDS:
            self.out.append("<br>")

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        text = "".join(self.out)
        while "<br><br><br>" in text:
            text = text.replace("<br><br><br>", "<br><br>")
        text = text.strip()
        while text.startswith("<br>"):
            text = text[4:].lstrip()
        while text.endswith("<br>"):
            text = text[:-4].rstrip()
        return text


def safe_description_html(markup: str | None) -> str:
    """Display-safe HTML for a stored description (see the module docstring)."""
    if not markup:
        return ""
    parser = _Sanitizer()
    parser.feed(markup)
    parser.close()
    return parser.result()


class _PlainText(HTMLParser):
    """Readable plain text from safe HTML, for terminals: headings and list items on their
    own lines, list items bulleted, links followed by their address."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "h2", "h3", "h4", "blockquote", "ul", "ol"}:
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n  - ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.parts.append(f" ({self.href})")
            self.href = None
        elif tag in {"p", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(safe_html: str) -> str:
    parser = _PlainText()
    parser.feed(safe_html)
    parser.close()
    lines = [" ".join(line.split()) if not line.startswith("  - ") else line.rstrip()
             for line in "".join(parser.parts).splitlines()]  # fmt: skip
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


@dataclass(frozen=True)
class Description:
    status: str  # "available", "empty", or "omitted_too_long"
    safe_html: str
    length: int
    source_note: str
    display_rules: str = DISPLAY_RULES_VERSION


_CATEGORY_NAMES = {
    "data_analysis": "Data analysis", "bi": "Business intelligence",
    "data_engineering": "Data engineering", "analytics_engineering": "Analytics engineering",
    "backend": "Backend", "software_engineering": "Software engineering",
    "full_stack": "Full stack", "data_science": "Data science", "ml": "Machine learning",
    "other": "Other", "uncertain": "Unclear role",
}  # fmt: skip
_SPONSORSHIP_TEXT = {
    "supported": "Visa sponsorship available",
    "not_supported": "No visa sponsorship",
    "not_stated": "Sponsorship not stated",
    "ambiguous": "Sponsorship unclear",
}


def _words(value: str) -> str:
    return value.replace("_", " ")


def _years(item: dict) -> str:
    lo, hi = item["min_years"], item["max_years"]
    fmt = lambda v: f"{v:g}"  # noqa: E731
    if lo is not None and hi is not None:
        return f"{fmt(lo)}-{fmt(hi)} years"
    if lo is not None:
        return f"{fmt(lo)}+ years"
    if hi is not None:
        return f"up to {fmt(hi)} years"
    return "experience"


def summary_lines(normalized: dict) -> list[str]:
    """Display-ready lines for an extraction result (role, requirements, eligibility,
    location). Groups keep the posting's own relationship wording ("Python or Java")."""
    n = normalized
    lines = []
    cats = [_CATEGORY_NAMES.get(c["category"], c["category"]) for c in n["role_categories"]]
    early = n.get("early_career")  # absent in answers made before schema 2026-10-28.2
    if early:
        reasons = ", ".join(_words(b) for b in early["basis"])
        label = {
            "yes": "Early career: yes",
            "internship": "Internship",
            "no": "Early career: no",
            "unclear": "Early career: unclear",
        }[early["status"]]
        lines.append(f"{label}{f' ({reasons})' if reasons else ''}")  # fmt: skip
    lines.append(
        f"Role: {', '.join(cats) or 'not stated'}; seniority "
        f"{_words(n['seniority']['value'])}; {_words(n['employment_type']['value'])}"
    )
    grouped = {m["canonical"] for g in n["skill_groups"] for m in g["members"]}
    for status, label in (("required", "Required"), ("preferred", "Preferred")):
        groups = [g["display_text"] for g in n["skill_groups"] if g["status"] == status]
        skills = [
            s["canonical"]
            for s in n["skills"]
            if s["status"] == status and s["canonical"] not in grouped
        ]
        if groups or skills:
            lines.append(f"{label}: {'; '.join(groups + skills)}")
    unclear = [s["canonical"] for s in n["skills"] if s["status"] == "ambiguous"]
    if unclear:
        lines.append(f"Requirement level unclear: {', '.join(unclear)}")
    alternatives = {a["group"]: a["display_text"] for a in n["alternatives"]}
    shown = set()
    for item in n["experience"]:
        group = item["alternative_group"]
        if group in alternatives:
            if group not in shown:
                shown.add(group)
                lines.append(f"Experience or education: {alternatives[group]}")
            continue
        subject = f" {item['subject']}" if item["subject"] else ""
        lines.append(f"Experience: {_years(item)}{subject} ({item['status']})")
    for item in n["education"]:
        group = item["alternative_group"]
        if group in alternatives:
            if group not in shown:
                shown.add(group)
                lines.append(f"Experience or education: {alternatives[group]}")
            continue
        fields = f" in {', '.join(item['fields'])}" if item["fields"] else ""
        lines.append(f"Education: {_words(item['level'])}{fields} ({item['status']})")
    if n["graduation"]["status"] != "not_stated":
        g = n["graduation"]
        lines.append(
            f"Graduation: {g['earliest'] or '?'} to {g['latest'] or '?'}"
            if g["status"] == "stated"
            else "Graduation: unclear"
        )
    lines.append(_SPONSORSHIP_TEXT[n["sponsorship"]["status"]])
    for key, label in (
        ("work_authorization", "Work authorization"),
        ("citizenship", "Citizenship"),
        ("clearance", "Clearance"),
    ):
        status = n[key]["status"]
        if status != "not_stated":
            level = f" ({n[key]['level']})" if key == "clearance" and n[key]["level"] else ""
            lines.append(f"{label}: {_words(status)}{level}")
    places = []
    for loc in n["locations"]:
        parts = [p for p in (loc["city"], loc["state"], loc["country"]) if p]
        places.append(", ".join(parts) if parts else loc["original"])
    arrangement = n["work_arrangement"]
    where = _words(arrangement["value"])
    if arrangement["onsite_days_per_week"] is not None:
        where += f", {arrangement['onsite_days_per_week']:g} day(s) a week on site"
    elif arrangement["onsite_frequency_text"]:
        where += f", {arrangement['onsite_frequency_text']}"
    lines.append(f"Where: {'; '.join(places) or 'location not stated'} ({where})")
    limits = [r["normalized_value"] for r in n["remote_restrictions"]]
    if limits:
        lines.append(f"Location limits: {'; '.join(limits)}")
    return lines  # fmt: skip


@dataclass(frozen=True)
class Extraction:
    status: str  # extracted, pending, running, failed, gave_up, skipped, superseded, not_queued
    result_id: int | None = None
    validation_status: str | None = None
    config_key: str | None = None
    summary: list[str] | None = None
    normalized: dict | None = None
    skip_reason: str | None = None


def _extraction(conn: psycopg.Connection, posting_id: int) -> Extraction:
    row = conn.execute(
        "SELECT result_id, validation_status, config_key, normalized FROM current_extractions "
        "WHERE posting_id = %s",
        (posting_id,),
    ).fetchone()
    if row:
        return Extraction("extracted", row[0], row[1], row[2], summary_lines(row[3]), row[3])
    job = conn.execute(
        "SELECT j.status, j.skip_reason, j.config_key FROM extraction_jobs j "
        "JOIN source_postings p ON p.current_version_id = j.version_id "
        "WHERE p.posting_id = %s ORDER BY j.job_id DESC LIMIT 1",
        (posting_id,),
    ).fetchone()
    if job is None:
        return Extraction("not_queued")
    return Extraction(job[0], config_key=job[2], skip_reason=job[1])


@dataclass(frozen=True)
class ListingDetail:
    posting_id: int
    employer: str
    source_system: str
    token: str
    source_posting_id: str
    title: str
    location_text: str | None
    locations: list[str]
    posting_url: str | None  # the source's posting page
    apply_url: str | None  # a separate application page, when the source has one
    availability_state: str
    qualifying_absences: int
    availability_changed_at: datetime | None
    first_discovered_at: datetime
    last_observed_at: datetime
    content_seen_at: datetime  # when this version of the content was first observed
    source_published_at: datetime | None
    source_created_at: datetime | None
    last_trusted_check_at: datetime | None
    retained: bool
    version_id: int
    version_no: int
    description: Description
    extraction: Extraction


def _locations(location_text: str | None, offices: Any) -> list[str]:
    found = [location_text] if location_text else []
    for office in offices or []:
        if isinstance(office, dict):
            value = office.get("location") or office.get("name")
            # Lever's workplace type is stored alongside offices; it is not a place.
            if (isinstance(value, str) and value and value not in found
                    and not value.startswith("Workplace type:")):  # fmt: skip
                found.append(value)
    return found


def listing_detail(conn: psycopg.Connection, posting_id: int) -> ListingDetail | None:
    """Everything a detail page needs for one listing, or None without stored content."""
    row = conn.execute(
        """
        SELECT l.posting_id, l.employer, l.source_system, l.token, l.source_posting_id,
               l.title, l.location_text, v.offices, l.absolute_url, l.apply_url,
               l.availability_state, l.qualifying_absences, l.availability_changed_at,
               l.first_discovered_at, l.last_observed_at, l.content_seen_at,
               l.source_published_at, l.source_created_at, bs.last_trusted_check_at,
               l.retained, l.version_id, l.version_no, v.content_html, v.description_text,
               v.description_length, v.description_omitted
        FROM listings l
        JOIN posting_versions v ON v.version_id = l.version_id
        JOIN source_postings p ON p.posting_id = l.posting_id
        JOIN board_status bs ON bs.board_id = p.board_id
        WHERE l.posting_id = %s
        """,
        (posting_id,),
    ).fetchone()
    if row is None:
        return None
    (pid, employer, source, token, sid, title, location, offices, url, apply, state, absences,
     changed, first, last, seen, published, created, trusted, retained, vid, vno, content_html,
     text, length, omitted) = row  # fmt: skip
    if omitted:
        description = Description("omitted_too_long", "", length or 0, SOURCE_NOTES[source])
    else:
        markup = content_html if content_html is not None else html.escape(text or "")
        safe = safe_description_html(markup)
        status = "available" if safe else "empty"
        description = Description(status, safe, length or 0, SOURCE_NOTES[source])
    return ListingDetail(
        pid, employer, source, token, sid, title, location, _locations(location, offices),
        safe_link(url), safe_link(apply), state, absences, changed, first, last, seen,
        published, created, trusted, retained, vid, vno, description,
        _extraction(conn, pid),
    )  # fmt: skip
