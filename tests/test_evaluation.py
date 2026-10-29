"""Evaluation cases: files, splits, the holdout guard, runs limited to cases, and
comparison kept apart by review state. The model is a scripted fake."""

import pytest
from extraction_fakes import ScriptedModel, answer, reply
from gh_fakes import T0, FakeClock, board, gh_job, registry
from test_extraction import CONFIG, X

from jobmarket import evaluation
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db


@pytest.fixture
def x(schema_conn):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    return X(schema_conn)


def postings(x, n):
    x.collect(T0, [gh_job(i, content=f"<p>Build pipelines with SQL, team {i}.</p>")
                   for i in range(1, n + 1)])  # fmt: skip
    return [r[0] for r in x.conn.execute("SELECT posting_id FROM source_postings ORDER BY 1")]


def selection(ids, splits):
    return [{"posting_id": p, "split": s, "bucket": f"b{i}"}
            for i, (p, s) in enumerate(zip(ids, splits, strict=True))]  # fmt: skip


def set_labels(path, review="proposed", **labels):
    text = path.read_text()
    text = text.replace('review = "proposed"', f'review = "{review}"')
    for key, value in labels.items():
        lines = [line for line in text.splitlines() if line.startswith(f"{key} = ")]
        text = text.replace(lines[0], f"{key} = {value}")
    path.write_text(text)


def test_prepare_writes_cases_with_the_exact_document_and_never_overwrites(x, tmp_path):
    ids = postings(x, 2)
    written = evaluation.prepare(x.conn, tmp_path, selection(ids, ["practice", "holdout"]))
    assert [p.name for p in written] == ["case-01.md", "case-02.md"]
    text = written[0].read_text()
    assert "Build pipelines with SQL, team 1." in text and 'review = "proposed"' in text
    set_labels(written[0], role_categories='["data_engineering"]')
    assert evaluation.prepare(x.conn, tmp_path, selection(ids, ["practice", "holdout"])) == []
    assert 'role_categories = ["data_engineering"]' in written[0].read_text()  # kept
    cases = evaluation.load_cases(tmp_path)
    assert [(c.case_id, c.split, c.review) for c in cases] == [
        ("case-01", "practice", "proposed"), ("case-02", "holdout", "proposed")]  # fmt: skip


def test_labels_must_be_complete_and_review_state_known(x, tmp_path):
    (path,) = evaluation.prepare(x.conn, tmp_path, selection(postings(x, 1), ["practice"]))
    set_labels(path, review="checked")
    with pytest.raises(ValueError, match="review must be one of"):
        evaluation.load_cases(tmp_path)
    lines = path.read_text().replace('review = "checked"', 'review = "proposed"').splitlines()
    path.write_text("\n".join(line for line in lines if not line.startswith("sponsorship =")))
    with pytest.raises(ValueError, match="missing labels"):
        evaluation.load_cases(tmp_path)


def test_the_holdout_needs_an_explicit_final_run(x, tmp_path):
    evaluation.prepare(x.conn, tmp_path, selection(postings(x, 2), ["practice", "holdout"]))
    cases = evaluation.load_cases(tmp_path)
    with pytest.raises(ValueError, match="kept unseen"):
        evaluation.run_cases(x.conn, ScriptedModel(), CONFIG, x.clock, x.sleep, cases,
                             "holdout")  # fmt: skip
    with pytest.raises(ValueError, match="kept unseen"):
        evaluation.compare(x.conn, CONFIG, cases, "holdout")


def test_a_run_extracts_only_the_split_and_leaves_the_queue_alone(x, tmp_path):
    ids = postings(x, 4)
    evaluation.prepare(x.conn, tmp_path, selection(ids[:2], ["practice", "holdout"]))
    cases = evaluation.load_cases(tmp_path)
    model = ScriptedModel(reply())
    report = evaluation.run_cases(x.conn, model, CONFIG, x.clock, x.sleep, cases, "practice")
    assert (report.requests, report.succeeded, len(model.calls)) == (1, 1, 1)
    assert "team 1." in model.calls[0]
    assert x.scalar("SELECT count(*) FROM extraction_results") == 1
    assert x.scalar("SELECT count(*) FROM extraction_jobs") == 1  # nothing else queued
    # Already extracted: nothing to do the second time.
    assert evaluation.run_cases(x.conn, model, CONFIG, x.clock, x.sleep, cases, "practice") is None


def test_comparison_keeps_reviewed_and_proposed_labels_apart(x, tmp_path):
    ids = postings(x, 2)
    paths = evaluation.prepare(x.conn, tmp_path, selection(ids, ["practice", "practice"]))
    labels = dict(role_categories='["data_engineering"]', required_skills='["Postgres", "SQL"]',
                  locations='["Dallas, Texas"]')  # fmt: skip
    set_labels(paths[0], review="human_reviewed", **labels)
    set_labels(paths[1], **labels)
    model_answer = answer(skills=[
        {"name": "SQL", "status": "required", "evidence": []},
        {"name": "PostgreSQL", "status": "required", "evidence": []},
        {"name": "Python", "status": "required", "evidence": []},
    ])  # fmt: skip
    evaluation.run_cases(x.conn, ScriptedModel(reply(model_answer)), CONFIG, x.clock, x.sleep,
                         evaluation.load_cases(tmp_path), "practice")  # fmt: skip
    comparisons = evaluation.compare(x.conn, CONFIG, evaluation.load_cases(tmp_path), "practice")
    first = comparisons[0]
    # Postgres and PostgreSQL are the same skill; Python is an extra the label does not have.
    assert first.set_counts["required_skills"] == (2, 1, 0)
    assert "locations" not in first.mismatches  # "Dallas, Texas" == "Dallas, TX"
    assert "role_categories" not in first.mismatches
    assert first.prompt_tokens == 1200
    summary = evaluation.summarize(comparisons)
    assert summary["human_reviewed"]["cases"] == 1 and summary["proposed"]["cases"] == 1
    assert summary["human_reviewed"]["fields"]["required_skills"]["agree"] == 0
    assert summary["missing_results"] == []
