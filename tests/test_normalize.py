"""Comparison-form rules: cosmetic differences are ignored, meaningful ones are kept."""

import pytest

from jobmarket import normalize
from jobmarket.normalize import (
    CURRENT_NORMALIZATION,
    SourceContent,
    UnknownNormalization,
    changed_fields,
    compare_form,
)

BASE_HTML = (
    "<h2>About the role</h2><p>Build data pipelines with SQL and Python.</p>"
    "<ul><li>3+ years of experience</li><li>Work with Snowflake</li></ul>"
    "<p>Pay range: $120,000 - $150,000 USD</p>"
)


def content(**overrides) -> SourceContent:
    values = dict(
        title="Data Engineer",
        location="Dallas, TX",
        content_html=BASE_HTML,
        offices=[{"name": "Dallas", "location": "Dallas, TX"}],
        departments=[{"name": "Data"}],
        metadata=None,
    )
    values.update(overrides)
    return SourceContent(**values)


def fp(**overrides) -> str:
    return compare_form(content(**overrides)).fingerprint


BASE = fp()


@pytest.mark.parametrize(
    "html",
    [
        # whitespace and line breaks
        BASE_HTML.replace("<p>Build", "<p>\n   Build").replace("Python.</p>", "Python.  </p>\n\n"),
        # different markup for the same words
        "<h3>About the role</h3>"
        "<div>Build data pipelines with <b>SQL</b> and <em>Python</em>.</div>"
        "<p>3+ years of experience<br>Work with Snowflake</p>"
        "<div class='pay'><span>Pay range: $120,000 - $150,000 USD</span></div>",
        # entities, non-breaking spaces, invisible characters, typographic dashes
        BASE_HTML.replace("SQL and", "SQL&nbsp;and").replace("with Snowflake", "with Snow​flake"),
        BASE_HTML.replace("$120,000 - $150,000", "$120,000 – $150,000"),
        # attributes, comments, scripts and styles are not visible text
        BASE_HTML.replace("<p>Build", '<p class="intro" style="color:red"><!-- edited -->Build')
        + "<style>p { margin: 0 }</style><script>track()</script>",
        # a typed bullet is the same as a list item
        BASE_HTML.replace("<li>3+ years", "<li>• 3+ years"),
    ],
)
def test_cosmetic_html_and_whitespace_differences_are_ignored(html):
    assert fp(content_html=html) == BASE


def test_cosmetic_title_and_location_differences_are_ignored():
    assert fp(title="  Data   Engineer ") == BASE
    assert fp(location="Dallas, TX") == BASE


def test_fields_outside_the_comparison_are_ignored():
    # Departments, URLs, and requisition ids are not part of the comparison form.
    assert fp(departments=[{"name": "Analytics"}]) == BASE
    assert fp(metadata=[{"name": "Cost Center", "value": "4410"}]) == BASE


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"title": "Senior Data Engineer"}, "title"),
        ({"title": "Data engineer"}, "title"),  # case is kept
        ({"content_html": BASE_HTML.replace("3+ years", "5+ years")}, "description"),
        ({"content_html": BASE_HTML.replace("Work with", "Do not work with")}, "description"),
        ({"content_html": BASE_HTML.replace("data pipe", "database pipe")}, "description"),
        ({"location": "Remote - US"}, "locations"),
        ({"offices": [{"name": "Austin", "location": "Austin, TX"}]}, "locations"),
        ({"content_html": BASE_HTML.replace("$150,000", "$160,000")}, "compensation"),
        (
            {"metadata": [{"name": "Salary Range", "value": "$120k-$150k"}]},
            "compensation",
        ),
        (
            {"metadata": [{"name": "Employment Type", "value": "Contract"}]},
            "employment_type",
        ),
        ({"title": "Data Engineer (Contract)"}, "employment_type"),
    ],
)
def test_meaningful_differences_are_kept(overrides, field):
    before = compare_form(content())
    after = compare_form(content(**overrides))
    assert after.fingerprint != before.fingerprint
    assert field in changed_fields(before.normalized, after.normalized)


def test_remote_restrictions_are_distinguished():
    assert fp(location="Remote - US") != fp(location="Remote - Texas")
    assert fp(location="Remote") != fp(location="Remote - US")


def test_compensation_and_employment_type_are_extracted():
    form = compare_form(
        content(
            content_html="<p>Employment type: Full-time</p><p>Base pay $95K to $110K per year.</p>",
            metadata=[
                {"name": "Pay Range", "value": {"unit": "USD", "amount": "95000"}},
                {"name": "Time Type", "value": "Full time"},
            ],
        )
    ).normalized
    stated = 'Pay Range: {"amount": "95000", "unit": "USD"}'
    assert form["compensation"] == [stated, "$95k", "$110k"]
    assert form["employment_type"] == ["full_time"]


def test_missing_html_falls_back_to_description_text():
    text = content(content_html=None, description_text="Build data pipelines.")
    html = content(content_html="<p>Build data pipelines.</p>")
    assert compare_form(text).fingerprint == compare_form(html).fingerprint


def test_rules_are_frozen():
    """Golden fingerprint. If this fails, a registered rule set was edited; add a new rules
    version instead, so old fingerprints stay reproducible."""
    form = compare_form(content(), "2026-10-26.1")
    assert form.normalization_version == "2026-10-26.1"
    assert form.fingerprint == GOLDEN_2026_10_26_1


GOLDEN_2026_10_26_1 = "d7ecaafbd7c93c73ad11ef913e34ea9bd68df37f6e7f51067920362fc709dd1d"


def test_current_rules_are_registered():
    assert CURRENT_NORMALIZATION in normalize.NORMALIZERS


def test_unknown_rules_are_refused():
    with pytest.raises(UnknownNormalization):
        compare_form(content(), "1999-01-01.1")
