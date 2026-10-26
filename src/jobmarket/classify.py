"""Rule-based screening of job postings.

Decides, from the title and the actual duties, whether a job belongs to a target family;
whether it is in Dallas-Fort Worth or Greater Houston, or remote and open to Texas
residents; its seniority; and what it says about visa sponsorship. Every decision records
its reasons so it can be reviewed. Change CLASSIFIER_VERSION whenever rules change: stored
screenings from an older version are recomputed on the next scan.
"""

import re
from dataclasses import dataclass, field
from typing import Any

CLASSIFIER_VERSION = "2026-10-26.1"

FAMILIES = (
    "data_engineering",
    "analytics_engineering",
    "data_analysis",
    "bi",
    "backend",
    "software_engineering",
    "full_stack",
    "data_science",
    "ml",
)

# --- Families ------------------------------------------------------------------------
# Strong title patterns place a job in a family on their own. Weak patterns need the
# description to show matching duties (see DUTY_TERMS).

_STRONG: dict[str, list[str]] = {
    "data_engineering": [
        r"\bdata (platform |infrastructure |integration |warehouse |pipeline )?engineer",
        r"\bbig data engineer",
        r"\betl (developer|engineer)",
        r"\bdata architect",
        r"\bdatabase (engineer|developer)",
    ],
    "analytics_engineering": [r"\banalytics engineer"],
    "data_analysis": [
        r"\bdata analys[ti]",
        r"\banalytics (analyst|manager|lead|specialist)",
        r"\b(manager|director|head|lead),? (of )?(data )?analytics\b",
        r"\bproduct analyst",
        r"\binsights? analyst",
        r"\breporting analyst",
        r"\bquantitative analyst",
        r"\bpeople analytics",
    ],
    "bi": [
        r"\bbusiness intelligence\b",
        r"\bbi (developer|engineer|analyst|architect|lead|manager)",
        r"\bpower ?bi\b",
        r"\btableau (developer|engineer|analyst)",
    ],
    "backend": [r"\bback[- ]?end\b", r"\bapi (developer|engineer)", r"\bserver[- ]side\b"],
    "full_stack": [r"\bfull[- ]?stack\b"],
    "software_engineering": [
        r"\bsoftware (development )?(engineer|developer|engineering)",
        r"\b(sde|swe)\b",
        r"\b(web|mobile|ios|android|front[- ]?end|java|python|golang|\.net|ruby|react|node(\.js)?|"
        r"embedded software|application|applications) (engineer|developer)",
        r"\bprogrammer\b",
        r"\bsoftware architect",
        r"\bsdet\b",
        r"\b(android|ios|mobile)\b.*\b(engineer|developer|lead)\b",
        r"\btech(nical)? lead\b.*\b(android|ios|mobile|backend|front[- ]?end|web|software)\b",
        r"\btest (development|automation) engineer",
        r"\bqa automation engineer",
        # Inverted titles: "Engineer, Software", "Engineer I, Software Test and Automation",
        # "Manager, Software - Perception".
        r"\bengineer( (i{1,3}|iv|[1-4]))?,\s*software\b",
        r"\b(manager|lead|director),\s*software\b",
    ],
    "data_science": [
        r"\bdata scien",
        r"\bapplied scientist",
        r"\b(machine learning|ml|ai) (research )?scientist",
        r"\bdecision scientist",
        r"\bstatistician\b",
    ],
    "ml": [
        r"\bmachine learning\b",
        r"\bml ?ops\b",
        r"\b(ml|ai|ai/ml|llm|genai|nlp) engineer",
        r"\bdeep learning\b",
        r"\bcomputer vision\b",
        r"\bai engineering\b",
        r"\breinforcement learning\b",
        r"\balgorithm engineer",
        r"\bagentic\b.*\bengineer",
    ],
}

_WEAK: dict[str, list[str]] = {
    "data_analysis": [
        r"\b(business|marketing|operations?|research|pricing|risk|strategy|revenue|revops|"
        r"customer|supply chain|commercial|growth|performance|sales operations|systems?|"
        r"planning|fundamentals|capacity|market) analyst",
        r"\banalytics\b",
    ],
    "software_engineering": [
        r"\b(platform|cloud|devops|site reliability|sre|infrastructure|systems|automation|"
        r"integration|solutions?|firmware|autonomy|robotics|simulation|perception|tools?) "
        r"(software )?(engineer|developer)",
        r"\bdeveloper\b",
        r"\bengineer(ing)? manager\b",
        r"\btech(nical)? lead\b",
        r"\bgpu engineer",
        r"\b(motion )?planning (and|&) control",
        r"\bcontrols? engineer",
        r"\bforward[- ]deployed (software )?engineer",
        r"\b(applications?|apps|solutions?|cloud|"
        r"ai|platform) architect",
    ],
    "data_engineering": [r"\bdata (&|and) ai\b", r"\bdata consultant"],
    "data_science": [r"\bresearch scientist", r"\bquantitative (researcher|developer)"],
}

# Duty evidence in the description. A weak title match needs at least this many
# distinct terms from the family's area.
MIN_DUTY_TERMS = 3
DUTY_TERMS: dict[str, list[str]] = {
    "data": [
        r"\bsql\b",
        r"\betl\b",
        r"\belt\b",
        r"data pipelines?",
        r"data warehous",
        r"\bdbt\b",
        r"\bairflow\b",
        r"\bspark\b",
        r"\bsnowflake\b",
        r"\bbigquery\b",
        r"\bredshift\b",
        r"\bdatabricks\b",
        r"\bkafka\b",
        r"data model",
        r"\btableau\b",
        r"power ?bi",
        r"\blooker\b",
        r"dashboards?",
        r"statistic",
        r"\bpython\b",
        r"\bpandas\b",
        r"a/b test",
        r"data visuali",
        r"\bexcel\b",
        r"\bkpis?\b",
    ],
    "software": [
        r"software development",
        r"write (clean |high[- ]quality |production )?code",
        r"\bapis?\b",
        r"microservices?",
        r"distributed systems",
        r"unit tests?",
        r"ci/cd",
        r"\bgit\b",
        r"\bjava\b",
        r"\bpython\b",
        r"\bgolang\b|\bgo\b(?= |,)",
        r"typescript",
        r"javascript",
        r"\breact\b",
        r"\bnode",
        r"c\+\+",
        r"\bc#",
        r"\.net\b",
        r"kubernetes",
        r"\baws\b|\bazure\b|\bgcp\b",
        r"code reviews?",
        r"\brust\b",
    ],
    "ml": [
        r"machine learning",
        r"model training|train(ing)? models",
        r"deep learning",
        r"\bpytorch\b",
        r"tensorflow",
        r"neural",
        r"computer vision",
        r"\bnlp\b",
        r"\bllms?\b",
        r"feature engineering",
        r"\bml\b",
    ],
}
_FAMILY_AREA = {
    "data_engineering": ("data",),
    "analytics_engineering": ("data",),
    "data_analysis": ("data",),
    "bi": ("data",),
    "backend": ("software",),
    "software_engineering": ("software",),
    "full_stack": ("software",),
    "data_science": ("data", "ml"),
    "ml": ("ml", "software"),
}

# The role itself is outside the target families, whatever tools the posting mentions
# (an accountant who uses SQL is still an accountant). Hard exclusions always apply.
_HARD_EXCLUSIONS = [
    r"\baccountant\b",
    r"\bbookkeep",
    r"\bauditor\b",
    r"\bpayroll\b",
    r"\brecruit",
    r"\btalent acquisition\b",
    r"\bnurse\b",
    r"\brn\b",
    r"\bphysician\b",
    r"\bpharmac",
    r"\btherapist\b",
    r"\bmedical assistant",
    r"\bteacher\b",
    r"\btutor\b",
    r"\binstructor\b",
    r"\bcashier\b",
    r"\btechnician\b",
    r"\belectrician\b",
    r"\bmechanic\b",
    r"\b(truck|cdl|delivery|safety) driver",
    r"\bdriver (cdl|class)",
    r"\bcdl\b",
    r"\bsuperintendent\b",
    r"\bforeman\b",
    r"\battorney\b",
    r"\bcounsel\b",
    r"\bparalegal\b",
    r"\bchef\b",
    r"\bcook\b",
    r"\bcustodian\b",
    r"\bjanitor",
    r"\bsecurity officer\b",
    r"\bpolice\b",
    r"\bfirefighter\b",
    r"\bsales engineer",
    r"\bpre-?sales\b",
    r"\baccount (executive|manager)",
    r"\bsdr\b",
    r"\bbusiness development\b",
    r"\bfield (service |sales )?engineer",
    r"\binstaller\b",
    r"\blineman\b",
    # Machining and plant-floor programming are not software engineering.
    r"\bcnc\b",
    r"\bplc (programmer|technician)\b",
    r"\bmachinist\b",
    r"\bmachine operator\b",
    # Survey, data-entry, and rating gigs mention "data" but are not data roles.
    r"\bdata entry\b",
    r"\bdata (collector|collection (specialist|associate|agent))\b",
    r"\bsurvey (taker|participant|respondent|panel(ist)?|interviewer|tester)\b",
    r"\bpaid (survey|research|study|studies|focus)\b",
    r"\bfocus group\b",
    r"\bmystery shop",
    r"\bproduct tester\b",
    r"\btranscri(ber|ptionist|ption)\b",
    r"\b(data )?annotat(or|ion)\b",
    r"\bsearch (engine )?evaluator\b",
    r"\b(online|internet|ads?|social media|map|maps) (rater|assessor|evaluator)\b",
    r"\bai (trainer|tutor|rater)\b",
]
# Soft exclusions describe a non-target domain; a clear target title still wins (for
# example "Senior Data Warehouse Engineer" or "Clinical Data Scientist").
_SOFT_EXCLUSIONS = [
    r"\baccounting\b",
    r"\btax\b",
    r"\bclinical\b",
    r"\bretail\b",
    r"\bstore (manager|associate)",
    r"\bwarehouse\b",
    r"\bconstruction\b",
    r"\bfacilit(y|ies) ",
    r"\bfinancial analyst",
    r"\bfp&a\b",
    r"\bcredit analyst",
    r"\bunderwrit",
    r"\bclaims\b",
    r"\bcontroller\b",
    # Engineering disciplines outside the target families. A clear target title still
    # wins (for example "Electrical Software Engineer" matches a strong pattern first).
    r"\bcommissioning\b",
    r"\bhvac\b",
    r"\bpiping\b",
    r"\bstructural\b",
    r"\bcivil\b",
    r"\bgeotechnical\b",
    r"\bmechanical\b",
    r"\belectrical\b",
    r"\bprocess engineer",
    r"\bmanufacturing engineer",
    r"\bproject engineer",
    r"\bstationary engineer",
    r"\bvalidation engineer",
    r"\bflight test",
    r"\baerodynamic",
    r"\bpropulsion\b",
    r"\bavionics\b",
    r"\bphysical infrastructure\b",
]
# Domain words that block only weak matches ("Marketing Analyst" needs data duties and is
# still excluded if it is really a marketing role); strong titles like "Sales Data
# Analyst" are kept.
_WEAK_BLOCKERS = [
    r"\bsales\b",
    r"\bmarketing manager",
    r"\bfinance\b",
    r"\bfinancial\b",
    r"\blegal\b",
    r"\bsupport engineer",
    r"\bcustomer support",
    r"\bapplication engineer.*\b(well|equipment|rig)",
]
# Not a specific vacancy.
_NOT_A_VACANCY = [
    r"talent (community|pool|network)",
    r"general (application|interest)",
    r"future opportunit",
    r"\bexpression of interest\b",
    r"join our talent",
    r"don'?t see (your|the) (role|job|position)",
    r"\bapply here\b",
    r"open application",
]


def _any(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text):
            return pattern
    return None


def _duty_terms(area: str, text: str) -> list[str]:
    return [p for p in DUTY_TERMS[area] if re.search(p, text)]


# --- Locations -----------------------------------------------------------------------
# (name, needs_texas_context). Names that also exist outside Texas, or that are common
# words, need ", TX" / "Texas" / a metro name next to them.
DFW_CITIES = [
    ("dallas", False),
    ("fort worth", False),
    # A misspelling some employers publish ("Forth Worth, Texas"); the source text is kept
    # as published and the screening reasons say the alias was used.
    ("forth worth", True),
    ("ft. worth", False),
    ("ft worth", False),
    ("arlington", True),
    ("plano", False),
    ("irving", True),
    ("frisco", True),
    ("richardson", True),
    ("coppell", False),
    ("addison", True),
    ("carrollton", True),
    ("garland", True),
    ("mckinney", True),
    ("allen", True),
    ("denton", True),
    ("lewisville", False),
    ("grapevine", True),
    ("southlake", False),
    ("flower mound", False),
    ("mesquite", True),
    ("grand prairie", False),
    ("las colinas", False),
    ("westlake", True),
    ("keller", True),
    ("euless", False),
    ("bedford", True),
    ("hurst", True),
    ("rockwall", False),
    ("prosper", True),
    ("the colony", True),
    ("farmers branch", False),
    ("roanoke", True),
    ("mansfield", True),
    ("north richland hills", False),
    ("colleyville", False),
    ("wylie", True),
    ("sachse", False),
    ("rowlett", False),
    ("cedar hill", True),
    ("duncanville", False),
    ("desoto", True),
    ("little elm", False),
    ("celina", True),
    ("haltom city", False),
    ("dfw", False),
    ("dallas-fort worth", False),
    ("dallas/fort worth", False),
    ("dallas fort worth", False),
]
HOUSTON_CITIES = [
    ("houston", False),
    ("the woodlands", False),
    ("woodlands", True),
    ("sugar land", False),
    ("katy", True),
    ("pearland", False),
    ("spring", True),
    ("humble", True),
    ("conroe", False),
    ("cypress", True),
    ("pasadena", True),
    ("baytown", False),
    ("league city", False),
    ("kingwood", False),
    ("tomball", False),
    ("missouri city", False),
    ("stafford", True),
    ("bellaire", True),
    ("friendswood", False),
    ("galveston", False),
    ("webster", True),
    ("richmond", True),
    ("rosenberg", True),
    ("channelview", False),
    ("deer park", True),
    ("la porte", True),
    ("seabrook", True),
    ("clear lake", True),
    ("greater houston", False),
]
_TEXAS = r"\b(tx|texas)\b"
_TEXAS_STATE = re.compile(r"\b(TX|Texas)\b")
_SPORTS = re.compile(
    r"\b(dallas|houston|fort worth|texas) (cowboys|mavericks|mavs|stars|rangers|wings|texans|"
    r"astros|rockets|dynamo|dash|roughnecks|sabercats)\b|\bfc dallas\b"
)
_PORTUGUESE_PLANO = re.compile(r"\bplano(s)? (de|odontol|médic|medic)")
_REMOTE = re.compile(r"\bremote\b|\bdistributed\b|work from home|\bwfh\b|\banywhere\b")
_US = re.compile(r"\b(us|u\.s\.|usa|united states|north america|americas|nationwide)\b")
_COUNTRIES = (
    "afghanistan|albania|algeria|andorra|angola|argentina|armenia|australia|austria|"
    "azerbaijan|bahamas|bahrain|bangladesh|barbados|belarus|belgium|belize|benin|bhutan|"
    "bolivia|bosnia|botswana|brazil|brasil|brunei|bulgaria|burkina faso|burundi|cambodia|"
    "cameroon|canada|chad|chile|china|colombia|congo|costa rica|croatia|cuba|cyprus|"
    "czech republic|czechia|denmark|djibouti|dominican republic|ecuador|egypt|el salvador|"
    "estonia|eswatini|ethiopia|fiji|finland|france|gabon|gambia|georgia \\(country\\)|germany|"
    "ghana|greece|guatemala|guinea|guyana|haiti|honduras|hong kong|hungary|iceland|india|"
    "indonesia|iran|iraq|ireland|israel|italy|ivory coast|jamaica|japan|jordan|kazakhstan|"
    "kenya|korea|kosovo|kuwait|kyrgyzstan|laos|latvia|lebanon|lesotho|liberia|libya|"
    "liechtenstein|lithuania|luxembourg|madagascar|malawi|malaysia|maldives|mali|malta|"
    "mauritania|mauritius|mexico|moldova|monaco|mongolia|montenegro|morocco|mozambique|"
    "myanmar|namibia|nepal|netherlands|new zealand|nicaragua|niger|nigeria|north macedonia|"
    "norway|oman|pakistan|panama|paraguay|peru|philippines|poland|portugal|puerto rico|qatar|"
    "romania|russia|rwanda|saudi arabia|senegal|serbia|singapore|slovakia|slovenia|somalia|"
    "south africa|spain|sri lanka|sudan|suriname|sweden|switzerland|syria|taiwan|tajikistan|"
    "tanzania|thailand|togo|trinidad|tunisia|turkey|türkiye|turkmenistan|uganda|ukraine|"
    "united arab emirates|uae|united kingdom|uk|england|scotland|wales|uruguay|uzbekistan|"
    "venezuela|vietnam|yemen|zambia|zimbabwe"
)
_NON_US = re.compile(
    r"\b(" + _COUNTRIES + r"|emea|europe|apac|latam|asia|africa|middle east|"
    r"bengaluru|bangalore|london|toronto|oslo|são paulo|sao paulo|lisbon|berlin|"
    r"amsterdam|dublin|tel aviv|vancouver|montreal|hyderabad|pune|manila)\b"
)
_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN UT VT VA WA WV WI WY DC"
).split()
_US_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    "oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|utah|vermont|"
    "virginia|washington|west virginia|wisconsin|wyoming"
)
_OTHER_STATE = re.compile(
    r"(?:,|\(|-|–|\b)\s*("
    + "|".join(_US_STATE_CODES)
    + r")\b(?![a-z])|\b("
    + _US_STATE_NAMES
    + r")\b"
)
# A location claim inside the description, e.g. "This role is based in Houston, TX".
_DESC_LOCATION_PHRASE = re.compile(
    r"(based|located|office|offices|onsite|on-site|on site|hybrid|in-office|in office|"
    r"headquartered|work|working|relocate|report) (in|at|out of|from|to) (our )?(the )?"
    r"(?P<place>[^.;:\n]{2,60})",
    re.IGNORECASE,
)
_DESC_TEXAS_REMOTE = re.compile(
    r"remote[^.\n]{0,80}\b(texas|tx)\b|"
    r"\b(texas|tx)\b[^.\n]{0,40}(resident|based)[^.\n]{0,40}remote|"
    r"(open to|eligible|hire|hiring)[^.\n]{0,60}(candidates|residents|people)"
    r"[^.\n]{0,40}\b(texas|tx)\b",
    re.IGNORECASE,
)
_DESC_TEXAS_EXCLUDED = re.compile(
    r"(cannot|can't|can not|unable to|not able to|do not|don't|does not|will not) "
    r"(currently )?(hire|employ|accept|consider)[^.\n]{0,120}\b(texas|tx)\b",
    re.IGNORECASE,
)


def _city_hits(text: str, cities: list[tuple[str, bool]]) -> list[str]:
    """Cities from the list found in a structured location string (lowercase)."""
    has_texas = re.search(_TEXAS, text) is not None
    metro_context = re.search(r"\b(dfw|dallas|houston|fort worth)\b", text) is not None
    hits = []
    for city, needs_context in cities:
        pattern = r"(?<![a-z])" + re.escape(city) + r"(?![a-z])"
        if not re.search(pattern, text):
            continue
        if city == "plano" and _PORTUGUESE_PLANO.search(text):
            continue
        if needs_context and not (has_texas or metro_context):
            continue
        hits.append(city)
    return hits


def _metros_in(text: str) -> set[str]:
    text = _SPORTS.sub(" ", text)
    found = set()
    if _city_hits(text, DFW_CITIES):
        found.add("dfw")
    if _city_hits(text, HOUSTON_CITIES):
        found.add("houston")
    return found


# --- Seniority and sponsorship -------------------------------------------------------

_SENIORITY = [
    ("intern", r"\bintern(ship)?\b|\bco-?op\b|\bsummer analyst\b"),
    ("manager", r"\bmanager\b|\bdirector\b|\bvp\b|\bvice president\b|\bhead of\b|\bchief\b"),
    ("lead", r"\blead\b|\bprincipal\b|\bstaff\b|\bdistinguished\b|\barchitect\b"),
    ("senior", r"\bsenior\b|\bsr\.?\b|\biii\b|\biv\b"),
    (
        "entry",
        r"\bnew grad|\bgraduate\b|\bentry[- ]level\b|\bjunior\b|\bjr\.?\b|\bassociate\b|"
        r"\bearly[- ]career\b|\b(engineer|analyst|developer|scientist) i\b|\b(level )?i$",
    ),
    ("mid", r"\bii\b|\bmid[- ]level\b|\bintermediate\b"),
]
_YEARS = re.compile(
    r"(?P<n>\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*years?(?P<ctx>[^.\n]{0,60})",
    re.IGNORECASE,
)

_SPONSOR_NOT = re.compile(
    r"(not|unable to|cannot|can't|won't|will not|do not|does not|don't|no longer)\s+"
    r"(be able to\s+)?(offer|provide|sponsor|support)[^.\n]{0,40}(sponsor|visa)|"
    r"(without|no)\s+(the need for\s+|need for\s+|requiring\s+|current or future\s+)?"
    r"(visa\s+)?sponsorship|sponsorship\s+(is\s+)?not\s+(available|offered|provided)|"
    r"not\s+eligible\s+for\s+(visa\s+)?sponsorship",
    re.IGNORECASE,
)
_SPONSOR_YES = re.compile(
    r"(visa\s+)?sponsorship\s+(is\s+)?(available|offered|provided)|"
    r"(we|will|can|able to)\s+(offer|provide)\s+(visa\s+)?sponsorship|"
    r"\bwe\s+(will\s+|can\s+)?sponsor\b|\bwill sponsor\b",
    re.IGNORECASE,
)
_SPONSOR_ASK = re.compile(
    r"(will you|do you|would you)[^?\n]{0,80}(require|need)[^?\n]{0,60}sponsorship[^?\n]{0,40}\?|"
    r"(now or in the future)[^.?\n]{0,40}(require|need)[^.?\n]{0,40}sponsorship",
    re.IGNORECASE,
)


def _snippet(text: str, match: re.Match, width: int = 120) -> str:
    start = max(0, match.start() - width // 2)
    return re.sub(r"\s+", " ", text[start : match.end() + width // 2]).strip()


@dataclass
class Screening:
    relevant: bool
    retained: bool
    families: list[str]
    texas_eligibility: str
    city_targeted_remote: bool
    seniority: str
    min_years_required: int | None
    early_career: bool
    sponsorship_status: str
    sponsorship_evidence: str | None
    needs_review: bool
    reasons: list[str] = field(default_factory=list)
    classifier_version: str = CLASSIFIER_VERSION

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def classify_relevance(title: str, description: str) -> tuple[bool, list[str], bool, list[str]]:
    """Return (relevant, families, needs_review, reasons)."""
    t = title.lower()
    d = description.lower()
    reasons: list[str] = []

    if (p := _any(_NOT_A_VACANCY, t)) is not None:
        return False, [], False, [f"not a specific vacancy (title matches {p!r})"]
    if (p := _any(_HARD_EXCLUSIONS, t)) is not None:
        return False, [], False, [f"role outside target families (title matches {p!r})"]

    strong = [f for f, pats in _STRONG.items() if _any(pats, t)]
    if strong:
        reasons.append("title: " + ", ".join(strong))
        return True, strong, False, reasons
    if (p := _any(_SOFT_EXCLUSIONS, t)) is not None:
        return False, [], False, [f"role outside target families (title matches {p!r})"]

    weak = [f for f, pats in _WEAK.items() if _any(pats, t)]
    if not weak:
        return False, [], False, ["title does not match a target family"]
    if (p := _any(_WEAK_BLOCKERS, t)) is not None:
        return False, [], False, [f"weak title match blocked by domain (title matches {p!r})"]

    confirmed, review = [], False
    for family in weak:
        terms = {term for area in _FAMILY_AREA[family] for term in _duty_terms(area, d)}
        if len(terms) >= MIN_DUTY_TERMS:
            confirmed.append(family)
            reasons.append(f"title suggests {family}; duties confirm ({len(terms)} terms)")
        elif terms:
            review = True
            reasons.append(f"title suggests {family}; only {len(terms)} duty term(s)")
        else:
            reasons.append(f"title suggests {family}; no matching duties")
    if confirmed:
        return True, confirmed, False, reasons
    return False, [], review, reasons


def classify_location(
    location_name: str | None, offices: list[dict[str, Any]], title: str, description: str
) -> tuple[str, bool, bool, list[str]]:
    """Return (texas_eligibility, city_targeted_remote, needs_review, reasons)."""
    location_text = location_name or ""
    office_pieces = [f"{o.get('name') or ''} {o.get('location') or ''}".strip() for o in offices]
    office_text = " | ".join(office_pieces)
    parts = [location_text, office_text, title]
    structured_original = " | ".join(p for p in parts if p)
    structured = structured_original.lower()
    reasons: list[str] = []

    # The posting's own location field outranks the employer's office list, which can
    # name every office the job is attached to. Each location is read on its own, so
    # "Arlington, VA" listed next to "Houston, TX" does not borrow that Texas context.
    loc_lower = location_text.lower()
    place_metros: set[str] = set()
    for piece in [location_text, *office_pieces]:
        if piece:
            place_metros |= _metros_in(piece.lower())
    title_metros = _metros_in(title.lower())
    metros = place_metros | title_metros
    remote = _REMOTE.search(structured) is not None
    texas_named = _TEXAS_STATE.search(structured_original) is not None

    if _DESC_TEXAS_EXCLUDED.search(description):
        return "not_texas", False, False, ["description says Texas residents cannot be hired"]

    if "forth worth" in structured:
        reasons.append("source spelling 'Forth Worth' read as Fort Worth")
    if place_metros:
        # A DFW/Houston office or work location is offered, even if remote is too.
        reasons.append("location fields name " + "/".join(sorted(place_metros)))
        return _local(place_metros), False, False, reasons
    if title_metros and not remote:
        reasons.append("title names " + "/".join(sorted(title_metros)))
        return _local(title_metros), False, False, reasons
    if remote:
        if metros or texas_named:
            # For example a "Remote" location with "- Dallas, TX" in the title: a remote
            # role aimed at that area, not a physical office.
            reasons.append("remote role targeted at Texas")
            city_targeted = bool(title_metros) or _TEXAS_STATE.search(title) is not None
            return "remote_texas", city_targeted, False, reasons
        if _DESC_TEXAS_REMOTE.search(description):
            reasons.append("description says remote is open to Texas")
            return "remote_texas", False, False, reasons
        other_states = {
            m.group(1) or m.group(2) for m in _OTHER_STATE.finditer(structured_original)
        }
        other_states.discard(None)
        primary = loc_lower + " | " + title.lower()
        if _NON_US.search(primary) and not _US.search(primary):
            return "not_texas", False, False, ["remote outside the US"]
        if _NON_US.search(structured) and not _US.search(structured):
            return "not_texas", False, False, ["remote outside the US"]
        if other_states:
            return (
                "not_texas",
                False,
                False,
                ["remote limited to other states: " + ", ".join(sorted(other_states))],
            )
        return "remote_ambiguous", False, True, ["remote role without explicit Texas eligibility"]

    location_only = " ".join(parts[:-1]).strip()
    if _names_a_place(location_only):
        # Concrete places outside DFW/Houston are authoritative; a company boilerplate line
        # such as "offices in Dallas and Toronto" does not move the job.
        return "not_texas", False, False, ["location fields name places outside DFW/Houston"]

    # The location fields name no concrete place (empty, a country, or a label such as a
    # department): look for an explicit location sentence in the description. That is
    # weaker evidence, so it is flagged for review.
    desc_metros: set[str] = set()
    for match in _DESC_LOCATION_PHRASE.finditer(description):
        desc_metros |= _metros_in(match.group("place").lower())
    if desc_metros:
        reasons.append("description location sentence names " + "/".join(sorted(desc_metros)))
        return _local(desc_metros), False, True, reasons
    if re.search(r"[a-z]", location_only.lower()):
        return "not_texas", False, False, ["location fields name places outside DFW/Houston"]
    return "unknown", False, False, ["no usable location information"]


_CITY_REGION = re.compile(r"[^\W\d_][\w .'-]*,\s*[^\W\d_]{2,}")


def _names_a_place(text: str) -> bool:
    """Whether location text names a concrete place: "City, Region", a US state, Texas, or
    a place outside the US. A bare country ("United States") or a label does not."""
    if not text:
        return False
    return bool(
        _CITY_REGION.search(text)
        or _OTHER_STATE.search(text)
        or _TEXAS_STATE.search(text)
        or (_NON_US.search(text.lower()) and not _US.search(text.lower()))
    )


def _local(metros: set[str]) -> str:
    if metros == {"dfw", "houston"}:
        return "local_both"
    return "local_dfw" if "dfw" in metros else "local_houston"


def classify_seniority(title: str, description: str) -> tuple[str, int | None, bool]:
    t = title.lower()
    seniority = "unknown"
    for level, pattern in _SENIORITY:
        if re.search(pattern, t):
            seniority = level
            break
    years = []
    for m in _YEARS.finditer(description):
        if "experience" in m.group("ctx").lower():
            n = int(m.group("n"))
            if n <= 30:
                years.append(n)
    min_years = min(years) if years else None
    early = seniority in ("intern", "entry") or (
        seniority in ("mid", "unknown") and min_years is not None and min_years <= 2
    )
    if seniority in ("senior", "lead", "manager"):
        early = False
    return seniority, min_years, early


def classify_sponsorship(description: str) -> tuple[str, str | None]:
    """not_stated unless the text actually says whether sponsorship is available.
    A question asking applicants about sponsorship is not evidence either way."""
    if m := _SPONSOR_NOT.search(description):
        return "not_available", _snippet(description, m)
    if m := _SPONSOR_YES.search(description):
        return "available", _snippet(description, m)
    if m := _SPONSOR_ASK.search(description):
        return "asked_only", _snippet(description, m)
    return "not_stated", None


def screen(
    title: str, description: str, location_name: str | None, offices: list[dict[str, Any]]
) -> Screening:
    relevant, families, review_rel, reasons = classify_relevance(title, description)
    eligibility, city_targeted, review_loc, loc_reasons = classify_location(
        location_name, offices, title, description
    )
    seniority, min_years, early = classify_seniority(title, description)
    sponsorship, evidence = classify_sponsorship(description)
    retained = relevant and eligibility in (
        "local_dfw",
        "local_houston",
        "local_both",
        "remote_texas",
        "remote_ambiguous",
    )
    needs_review = (
        review_rel or (relevant and review_loc) or (relevant and eligibility == "unknown")
    )
    if relevant and not retained:
        loc_reasons.append("relevant role but not available in DFW/Houston or to Texas residents")
    return Screening(
        relevant=relevant,
        retained=retained,
        families=families,
        texas_eligibility=eligibility,
        city_targeted_remote=city_targeted,
        seniority=seniority,
        min_years_required=min_years,
        early_career=early,
        sponsorship_status=sponsorship,
        sponsorship_evidence=evidence,
        needs_review=needs_review,
        reasons=reasons + loc_reasons,
    )
