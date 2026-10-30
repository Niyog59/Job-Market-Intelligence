"""Explainable comparisons (jobmarket.web.compare): stored requirements versus a confirmed
profile. Answers are normalized with the real Module 7 rules; profiles are fictional."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from extraction_fakes import answer

pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")

from jobmarket.extraction_check import derive, normalize_answer  # noqa: E402
from jobmarket.listings import Extraction  # noqa: E402
from jobmarket.web.compare import GAP, MATCHED, REVIEW, compare  # noqa: E402
from jobmarket.workspace import Education, Experience, Profile, Skill  # noqa: E402

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)


def listing(status="extracted", version_no=2, **changes):
    if status != "extracted":
        return SimpleNamespace(extraction=Extraction(status), version_no=version_no)
    normalized = normalize_answer(answer(**changes))
    return SimpleNamespace(
        extraction=Extraction("extracted", normalized=normalized, derived=derive(normalized)),
        version_no=version_no,
    )


def profile(**kw):
    kw.setdefault("version", 3)
    return Profile(**kw)


def results(comparison, section=None):
    return {c.requirement: c.result for c in comparison.checks
            if section is None or c.section == section}  # fmt: skip


def skill(name, status):
    return {"name": name, "status": status, "evidence": []}


def years(n, scope="overall", status="required", group=None, subject=None):
    return {"scope": scope, "subject": subject, "min_years": n, "max_years": None,
            "status": status, "alternative_group": group, "evidence": []}  # fmt: skip


def degree(level, status="required", group=None, fields=()):
    return {"level": level, "fields": list(fields), "status": status,
            "alternative_group": group, "evidence": []}  # fmt: skip


def job_months(start, end, kind="employment", current=False):
    return Experience(kind, f"Fictional {kind}", start=start, end=end, current=current)


EITHER = dict(
    skills=[skill("Python", "required"), skill("Java", "required"), skill("SQL", "required"),
            skill("Airflow", "preferred")],
    skill_groups=[{"relation": "any_of", "min_count": None, "members": ["Python", "Java"],
                   "status": "required", "display_text": "Python or Java", "evidence": []}],
)  # fmt: skip


def test_one_alternative_is_enough_and_missing_skills_are_not_gaps():
    c = compare(listing(**EITHER), profile(skills=[Skill("python")]), [], NOW)
    required = results(c, "Required skills")
    assert required == {"Python or Java": MATCHED, "SQL": REVIEW}
    either = next(x for x in c.checks if x.requirement == "Python or Java")
    assert "one of them is enough" in either.explanation
    sql = next(x for x in c.checks if x.requirement == "SQL")
    assert "Not listed in your profile" in sql.explanation and "does not mean you lack" in (
        sql.explanation
    )
    assert results(c, "Preferred skills") == {"Apache Airflow": REVIEW}  # kept apart from required
    assert GAP not in results(c).values()


def test_skill_aliases_match_their_canonical_names():
    c = compare(listing(skills=[skill("PostgreSQL", "required")]),
                profile(skills=[Skill("postgres")]), [], NOW)  # fmt: skip
    assert results(c, "Required skills") == {"PostgreSQL": MATCHED}


def test_years_count_employment_only_with_overlaps_once():
    two_years = dict(experience=[years(2)])
    overlapping = [
        job_months(date(2023, 1, 1), date(2023, 12, 1)),
        job_months(date(2023, 6, 1), date(2024, 6, 1)),
    ]  # 18 distinct months
    short = compare(listing(**two_years), profile(experiences=overlapping), [], NOW)
    gap = next(x for x in short.checks if x.section == "Experience")
    assert gap.result == GAP and "1 year 6 months" in gap.explanation
    with_project = overlapping + [job_months(date(2020, 1, 1), date(2023, 1, 1), "project")]
    assert (
        next(
            x
            for x in compare(
                listing(**two_years), profile(experiences=with_project), [], NOW
            ).checks
            if x.section == "Experience"
        ).result
        == GAP
    )
    with_intern = overlapping + [job_months(date(2022, 1, 1), date(2022, 12, 1), "internship")]
    review = next(x for x in compare(listing(**two_years), profile(experiences=with_intern), [],
                                     NOW).checks if x.section == "Experience")  # fmt: skip
    assert review.result == REVIEW and "Internships" in review.explanation
    enough = [job_months(date(2022, 1, 1), None, current=True)]
    assert (
        next(
            x
            for x in compare(listing(**two_years), profile(experiences=enough), [], NOW).checks
            if x.section == "Experience"
        ).result
        == MATCHED
    )


def test_skill_specific_experience_needs_review():
    c = compare(
        listing(experience=[years(3, scope="skill", subject="Spark")]),
        profile(experiences=[job_months(date(2015, 1, 1), None, current=True)]),
        [],
        NOW,
    )
    assert [x.result for x in c.checks if x.section == "Experience"] == [REVIEW]


def test_education_levels_and_expected_degrees():
    bachelor = dict(education=[degree("bachelor")])

    def result(*entries):
        c = compare(listing(**bachelor), profile(education=list(entries)), [], NOW)
        return next(x.result for x in c.checks if x.section == "Education")

    assert result() == REVIEW  # nothing listed
    assert result(Education("master", status="completed", graduation=date(2024, 5, 1))) == MATCHED
    assert result(Education("bachelor", status="expected", graduation=date(2027, 5, 1))) == REVIEW
    assert result(Education("associate", status="completed", graduation=date(2022, 5, 1))) == GAP
    assert result(Education("certificate", status="completed")) == REVIEW  # not comparable


def test_alternative_paths_are_met_by_either_and_never_become_gaps():
    alt = dict(
        education=[degree("bachelor", group="a")],
        experience=[years(4, group="a")],
        alternatives=[{"group": "a", "display_text": "a bachelor's degree or 4 years of "
                       "experience", "evidence": []}],
    )  # fmt: skip
    worker = profile(experiences=[job_months(date(2019, 1, 1), None, current=True)])
    met = compare(listing(**alt), worker, [], NOW)
    assert results(met, "Experience or education") == {
        "a bachelor's degree or 4 years of experience": MATCHED
    }
    neither = compare(listing(**alt), profile(education=[Education(
        "associate", status="completed", graduation=date(2023, 5, 1))]), [], NOW)  # fmt: skip
    assert list(results(neither, "Experience or education").values()) == [REVIEW]
    assert not any(x.section in ("Experience", "Education") for x in neither.checks)


def test_graduation_windows():
    window = dict(graduation={"status": "stated", "earliest": "2026-12", "latest": "2027-06",
                              "evidence": []})  # fmt: skip

    def result(month):
        edu = [Education("bachelor", status="expected", graduation=month)] if month else []
        c = compare(listing(**window), profile(education=edu), [], NOW)
        return next(x.result for x in c.checks if x.section == "Graduation")

    assert result(date(2027, 5, 1)) == MATCHED
    assert result(date(2028, 5, 1)) == GAP
    assert result(None) == REVIEW


def test_preferences_are_gaps_labeled_as_preferences():
    seattle = dict(locations=[{"original": "Seattle, WA", "city": "Seattle", "state": "WA",
                               "country": None, "evidence": []}])  # fmt: skip
    dallas = dict(locations=[{"original": "Dallas, TX", "city": "Dallas", "state": "TX",
                              "country": None, "evidence": []}])  # fmt: skip
    wants_dfw = profile(preferred_places=["dfw"])
    outside = next(x for x in compare(listing(**seattle), wants_dfw, [], NOW).checks
                   if x.requirement == "Location")  # fmt: skip
    assert outside.result == GAP and "Outside your preferences" in outside.explanation
    inside = next(x for x in compare(listing(**dallas), wants_dfw, [], NOW).checks
                  if x.requirement == "Location")  # fmt: skip
    assert inside.result == MATCHED
    no_prefs = compare(listing(**seattle), profile(), [], NOW)
    assert not any(x.section == "Preferences" for x in no_prefs.checks)


def test_eligibility_is_shown_as_statements_only():
    stated = dict(
        sponsorship={"status": "not_supported", "evidence": []},
        clearance={"status": "required", "level": "Secret", "evidence": []},
    )
    c = compare(listing(**stated), profile(highlight_sponsorship=True), [], NOW)
    assert [s.label for s in c.statements] == ["Sponsorship", "Clearance"]
    assert c.statements[0].highlighted and not c.statements[1].highlighted
    assert not any("ponsor" in x.section or "learance" in x.section for x in c.checks)
    silent = compare(listing(), profile(), [], NOW)
    assert silent.statements == []  # not stated: nothing shown, nothing assumed


def test_no_comparison_without_a_current_extraction_or_a_profile():
    pending = compare(listing("pending"), profile(), [], NOW)
    assert not pending.available and "has not read this version" in pending.reason
    assert pending.checks == []
    unsaved = compare(listing(**EITHER), Profile(), [], NOW)
    assert not unsaved.available and "Save your profile" in unsaved.reason


def test_versions_used_are_reported():
    c = compare(listing(version_no=4, **EITHER), profile(version=7), [], NOW)
    assert (c.profile_version, c.description_version) == (7, 4)
