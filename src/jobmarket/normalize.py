"""Comparison form of a posting's content: the test for whether content changed meaningfully.

The comparison form keeps what a job seeker would care about: title, description wording,
locations (which carry remote restrictions such as "Remote - US"), published compensation,
and employment type. It ignores HTML markup, whitespace and line breaks, invisible
characters, typographic variants of quotes and dashes, and every timestamp. Letter case,
numbers, and wording are kept, so normalization never merges two meaningfully different
texts.

Rules are versioned. Every version stays registered in NORMALIZERS so that a fingerprint
recorded under older rules can be reproduced exactly. Changing the rules means adding a new
function and a new version string, never editing a registered one; the golden fingerprint
test guards against accidental edits.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# Format YYYY-MM-DD.N, so versions sort chronologically as text.
CURRENT_NORMALIZATION = "2026-10-26.1"


class UnknownNormalization(Exception):
    """A fingerprint was recorded under rules that are no longer registered."""


@dataclass(frozen=True)
class SourceContent:
    """The original fields a comparison form is computed from. Built from a live job record
    or from a stored posting_versions row, so stored versions can be re-normalized."""

    title: str
    location: str | None
    content_html: str | None
    offices: list[dict[str, Any]] = field(default_factory=list)
    departments: list[dict[str, Any]] = field(default_factory=list)
    metadata: Any = None
    description_text: str | None = None  # used only when content_html is missing


@dataclass(frozen=True)
class Comparison:
    normalization_version: str
    normalized: dict[str, Any]
    fingerprint: str


# --- text clean-up -------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)
_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "″": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        # List bullets typed as characters are layout, like <li>.
        "•": " ",
        "▪": " ",
        "●": " ",
        "·": " ",
    }
)
_WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # also turns non-breaking spaces into spaces
    text = text.translate(_INVISIBLE).translate(_TYPOGRAPHY)
    return _WHITESPACE.sub(" ", text).strip()


class _VisibleText(HTMLParser):
    """Visible text of an HTML fragment. Block boundaries become spaces so words on either
    side never run together; script and style contents are dropped."""

    BLOCK_TAGS = {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "td", "th", "table", "section", "article", "header", "footer", "hr",
        "blockquote", "pre", "dl", "dt", "dd",
    }  # fmt: skip
    SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skipping += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def _visible_text(markup: str) -> str:
    parser = _VisibleText()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


# --- compensation and employment type --------------------------------------------------------

_COMPENSATION_FIELD = re.compile(r"salary|compensation|\bpay\b|pay range|wage|\bote\b", re.I)
_EMPLOYMENT_FIELD = re.compile(
    r"(employ\w*|time|worker|position|job|contract|work)\s*type|employment", re.I
)
_MONEY = re.compile(
    r"[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM]\b)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|CAD|EUR|GBP)\b"
)
_EMPLOYMENT_LABEL = re.compile(
    r"\b(?:employment|job|position|worker|time|contract)\s+type\s*[:\-]\s*"
    r"([A-Za-z][A-Za-z /\-]{2,40})",
    re.I,
)
_EMPLOYMENT_TERMS = (
    ("full_time", re.compile(r"\bfull[\s-]?time\b", re.I)),
    ("part_time", re.compile(r"\bpart[\s-]?time\b", re.I)),
    ("contract", re.compile(r"\bcontract(?:or)?\b|\b1099\b|\bc2c\b|\bcorp-to-corp\b", re.I)),
    ("temporary", re.compile(r"\btemp(?:orary)?\b", re.I)),
    ("internship", re.compile(r"\bintern(?:ship)?\b|\bco-?op\b", re.I)),
    ("fixed_term", re.compile(r"\bfixed[\s-]?term\b", re.I)),
    ("seasonal", re.compile(r"\bseasonal\b", re.I)),
)


def _metadata_fields(metadata: Any) -> list[tuple[str, Any]]:
    """Greenhouse custom fields: a list of {"name": ..., "value": ...} objects."""
    if not isinstance(metadata, list):
        return []
    fields = []
    for item in metadata:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            value = item.get("value")
            if value not in (None, "", [], {}):
                fields.append((_clean(item["name"]), value))
    return fields


def _canonical_value(value: Any) -> str:
    if isinstance(value, str):
        return _clean(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _employment_terms(text: str) -> set[str]:
    return {name for name, pattern in _EMPLOYMENT_TERMS if pattern.search(text)}


def _compensation(description: str, fields: list[tuple[str, Any]]) -> list[str]:
    amounts = []
    for match in _MONEY.finditer(description):
        amount = match.group(0).replace(" ", "").replace(",", "")
        amounts.append(amount[:-1] + amount[-1].lower() if amount[-1] in "KM" else amount)
    stated = [
        f"{name}: {_canonical_value(value)}"
        for name, value in fields
        if _COMPENSATION_FIELD.search(name)
    ]
    return sorted(stated) + amounts  # amounts keep their order: "$90k - $120k" is a range


def _employment_type(title: str, description: str, fields: list[tuple[str, Any]]) -> list[str]:
    found = _employment_terms(title)
    for match in _EMPLOYMENT_LABEL.finditer(description):
        label = " ".join(_clean(match.group(1)).split()[:3])
        found |= _employment_terms(label) or {f"stated: {label}"}
    for name, value in fields:
        if _EMPLOYMENT_FIELD.search(name) and not _COMPENSATION_FIELD.search(name):
            text = _canonical_value(value)
            found |= _employment_terms(text) or {f"{name}: {text}"}
    return sorted(found)


# --- rule versions ---------------------------------------------------------------------------


def _locations(content: SourceContent) -> list[str]:
    found = set()
    if content.location:
        found.add(_clean(content.location))
    for office in content.offices or []:
        value = office.get("location") or office.get("name") if isinstance(office, dict) else None
        if isinstance(value, str):
            found.add(_clean(value))
    return sorted(v for v in found if v)


def _comparison_2026_10_26_1(content: SourceContent) -> dict[str, Any]:
    title = _clean(content.title)
    if content.content_html is not None:
        description = _clean(_visible_text(content.content_html))
    else:
        description = _clean(content.description_text or "")
    fields = _metadata_fields(content.metadata)
    return {
        "title": title,
        "description": description,
        "locations": _locations(content),
        "compensation": _compensation(description, fields),
        "employment_type": _employment_type(title, description, fields),
    }


Normalizer = Callable[[SourceContent], dict[str, Any]]

NORMALIZERS: dict[str, Normalizer] = {
    "2026-10-26.1": _comparison_2026_10_26_1,
}


def fingerprint(normalized: dict[str, Any]) -> str:
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compare_form(content: SourceContent, version: str | None = None) -> Comparison:
    """The comparison form and fingerprint of content under one version of the rules
    (the current rules by default)."""
    version = version or CURRENT_NORMALIZATION
    try:
        normalizer = NORMALIZERS[version]
    except KeyError:
        raise UnknownNormalization(f"normalization rules {version!r} are not registered") from None
    normalized = normalizer(content)
    return Comparison(version, normalized, fingerprint(normalized))


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
