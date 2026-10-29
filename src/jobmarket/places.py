"""Deterministic clean-up of locations returned by requirement extraction.

The model reads location wording; this module only tidies what it returns: US state names
become two-letter codes, common country names become one spelling, and a Texas city is
placed in the DFW or Houston area using the same city lists as screening
(jobmarket.classify). Nothing is invented: an unknown state or country is kept as written,
a missing value stays missing, and no coordinates are produced.
"""

import re

from jobmarket.classify import DFW_CITIES, HOUSTON_CITIES

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "washington dc": "DC",
    "washington d.c.": "DC", "puerto rico": "PR",
}  # fmt: skip
_STATE_CODES = set(US_STATES.values())

# Canadian provinces and territories, which postings name like US states ("Edmonton, AB").
CA_PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "québec": "QC", "saskatchewan": "SK",
    "northwest territories": "NT", "nunavut": "NU", "yukon": "YT",
}  # fmt: skip

COUNTRIES = {
    "united states": "United States", "united states of america": "United States",
    "usa": "United States", "us": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "america": "United States",
    "canada": "Canada", "mexico": "Mexico", "méxico": "Mexico",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "u.k.": "United Kingdom",
    "gb": "United Kingdom",
    "great britain": "United Kingdom", "england": "United Kingdom",
    "india": "India", "germany": "Germany", "france": "France", "ireland": "Ireland",
    "netherlands": "Netherlands", "the netherlands": "Netherlands", "spain": "Spain",
    "poland": "Poland", "portugal": "Portugal", "brazil": "Brazil", "argentina": "Argentina",
    "colombia": "Colombia", "philippines": "Philippines", "singapore": "Singapore",
    "australia": "Australia", "japan": "Japan", "israel": "Israel", "china": "China",
}  # fmt: skip

_SPACES = re.compile(r"\s+")


def _key(value: str) -> str:
    return _SPACES.sub(" ", value).strip().strip(",").lower()


def state_code(state: str | None) -> str | None:
    """Two-letter code for a US state or Canadian province name or code; anything else is
    returned as written."""
    if not state or not state.strip():
        return None
    key = _key(state)
    if key.upper() in _STATE_CODES:
        return key.upper()
    return US_STATES.get(key) or CA_PROVINCES.get(key) or _SPACES.sub(" ", state).strip()


def country_name(country: str | None) -> str | None:
    if not country or not country.strip():
        return None
    return COUNTRIES.get(_key(country), _SPACES.sub(" ", country).strip())


def _area_cities(entries) -> set[str]:
    return {name for name, _alias in entries}


_DFW = _area_cities(DFW_CITIES)
_HOUSTON = _area_cities(HOUSTON_CITIES)


def metro_area(city: str | None, state: str | None) -> str | None:
    """'dfw' or 'houston' for a Texas city on the screening lists, otherwise None."""
    if not city or state_code(state) != "TX":
        return None
    key = _key(city)
    if key in _DFW:
        return "dfw"
    if key in _HOUSTON:
        return "houston"
    return None


def normalize_location(city: str | None, state: str | None, country: str | None) -> dict:
    code = state_code(state)
    nation = country_name(country)
    if nation is None and code in _STATE_CODES:
        nation = "United States"  # a US state code names the country; nothing else is added
    tidy_city = _SPACES.sub(" ", city).strip() if city and city.strip() else None
    return {
        "city": tidy_city,
        "state": code,
        "country": nation,
        "metro": metro_area(tidy_city, code),
    }
