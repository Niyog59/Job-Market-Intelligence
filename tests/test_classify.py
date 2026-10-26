import pytest

from jobmarket.classify import (
    classify_location,
    classify_relevance,
    classify_seniority,
    classify_sponsorship,
    screen,
)

DATA = "Build data pipelines in SQL and Python, model data in dbt, and own Tableau dashboards."
SOFTWARE = "Design APIs and microservices in Java and Python, write unit tests, run code reviews."


def office(name, location=None):
    return [{"name": name, "location": location or name}]


# --- relevance -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,family",
    [
        ("Senior Data Engineer", "data_engineering"),
        ("Analytics Engineer", "analytics_engineering"),
        ("Data Analyst II", "data_analysis"),
        ("Business Intelligence Developer", "bi"),
        ("Backend Engineer, Payments", "backend"),
        ("Software Engineer", "software_engineering"),
        ("Full-Stack Developer", "full_stack"),
        ("Data Scientist", "data_science"),
        ("Machine Learning Engineer", "ml"),
        ("Senior Data Warehouse Engineer", "data_engineering"),
    ],
)
def test_target_titles(title, family):
    relevant, families, _, _ = classify_relevance(title, "")
    assert relevant and family in families


def test_sql_and_python_do_not_make_accounting_relevant():
    relevant, _, _, reasons = classify_relevance(
        "Staff Accountant", "Use SQL, Python, and Excel dashboards for the monthly close."
    )
    assert not relevant and "accountant" in reasons[0]


@pytest.mark.parametrize(
    "title",
    [
        "Engineer",
        "Research Scientist, Chemistry",
        "Data Center Technician",
        "Data Center Construction Manager",
        "Mechanical Engineer",
        "Field Service Engineer",
    ],
)
def test_engineer_scientist_or_data_center_alone_do_not_qualify(title):
    relevant, _, _, _ = classify_relevance(title, "Hands-on lab and site work.")
    assert not relevant


def test_weak_title_needs_matching_duties():
    assert classify_relevance("Operations Analyst", DATA)[0]
    relevant, _, review, _ = classify_relevance("Operations Analyst", "Coordinate SQL vendors.")
    assert not relevant and review
    assert not classify_relevance("Operations Analyst", "Schedule the warehouse shifts.")[0]


def test_talent_pools_are_not_vacancies():
    assert not classify_relevance("Join Our Talent Community", DATA)[0]
    assert not classify_relevance("Don't See Your Role? Apply Here!", DATA)[0]


# --- location ------------------------------------------------------------------------


def loc(location, title="Data Engineer", description="", offices=None):
    return classify_location(location, offices or [], title, description)[0]


def test_local_offices():
    assert loc("Dallas, TX") == "local_dfw"
    assert loc("Plano, Texas, United States") == "local_dfw"
    assert loc("The Woodlands, TX") == "local_houston"
    assert loc("Houston, TX or San Francisco Bay Area") == "local_houston"
    assert loc("Fort Worth, Texas; Ann Arbor, Michigan") == "local_dfw"


def test_arlington_virginia_is_not_dfw():
    assert loc("Arlington, VA") == "not_texas"
    assert loc("Arlington, TX") == "local_dfw"


def test_sports_teams_and_portuguese_plano_are_not_locations():
    footer = "Proud partner of the Dallas Cowboys and the Houston Rockets."
    assert loc("New York, NY", description=footer) == "not_texas"
    assert loc("São Paulo, Brazil", description="Oferecemos plano de saúde.") == "not_texas"
    assert loc("", description="Benefícios: plano de saúde e plano odontológico.") == "unknown"


def test_route_mentions_do_not_establish_location():
    description = "Our trucks run autonomous routes between Dallas and Houston every day."
    assert loc("San Francisco, CA", description=description) == "not_texas"


def test_description_location_sentence_is_flagged_for_review():
    eligibility, _, review, _ = classify_location(
        "United States", [], "Data Engineer", "This role is based in our Houston, TX office."
    )
    assert eligibility == "local_houston" and review


def test_remote_eligibility():
    assert loc("Remote - Texas") == "remote_texas"
    assert loc("Remote (US)") == "remote_ambiguous"
    assert loc("Remote - CA, NY, WA") == "not_texas"
    assert loc("Remote - Canada") == "not_texas"
    assert (
        loc("Remote", description="We can only hire remote employees in Texas or Ohio.")
        == "remote_texas"
    )
    assert loc("Remote - US", description="We cannot hire in Texas at this time.") == "not_texas"


def test_city_targeted_remote_posting():
    eligibility, targeted, _, _ = classify_location(
        "Remote",
        [{"name": "Distributed", "location": "Remote"}],
        "Software Engineer - Dallas, TX, USA",
        "",
    )
    assert eligibility == "remote_texas" and targeted


def test_remote_option_with_local_office_counts_as_local():
    eligibility, targeted, _, _ = classify_location(
        "Remote, U.S, Ann Arbor, MI, Fort Worth, TX", [], "ML Engineer", ""
    )
    assert eligibility == "local_dfw" and not targeted


# --- seniority and sponsorship -------------------------------------------------------


def test_seniority_and_early_career():
    assert classify_seniority("Data Analyst (New Grad)", "")[::2] == ("entry", True)
    assert classify_seniority("Software Engineer Intern", "")[::2] == ("intern", True)
    assert classify_seniority("Senior Data Engineer", "1+ years of experience")[::2] == (
        "senior",
        False,
    )
    assert classify_seniority("Data Engineer", "0-2 years of experience with SQL") == (
        "unknown",
        0,
        True,
    )
    assert classify_seniority("Data Engineer", "5+ years of professional experience") == (
        "unknown",
        5,
        False,
    )


def test_sponsorship_never_guessed():
    assert classify_sponsorship(DATA) == ("not_stated", None)
    status, evidence = classify_sponsorship("We are unable to sponsor visas for this role.")
    assert status == "not_available" and "sponsor" in evidence
    assert classify_sponsorship("Visa sponsorship is available.")[0] == "available"
    asked = "Will you now or in the future require sponsorship for employment visa status?"
    assert classify_sponsorship(asked)[0] == "asked_only"


def test_screen_retains_relevant_texas_jobs_only():
    assert screen("Data Engineer", DATA, "Dallas, TX", []).retained
    assert not screen("Data Engineer", DATA, "London, United Kingdom", []).retained
    assert not screen("Staff Accountant", DATA, "Dallas, TX", []).retained
    ambiguous = screen("Backend Engineer", SOFTWARE, "Remote (US)", [])
    assert ambiguous.retained and ambiguous.needs_review
