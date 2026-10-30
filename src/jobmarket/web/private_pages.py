"""The owner's private pages: profile, saved jobs, applications, and the private parts of a
job page. Every route here checks the session on the server (auth.require_owner) and reads
or writes the private schema only through the private login, scoped to the signed-in
account. Public job data comes from the read-only connection; the two are combined in Python,
never in SQL, so neither login needs the other's tables.
"""

from datetime import date

from flask import abort, g, redirect, render_template, request, url_for
from psycopg import sql

from jobmarket import search, workspace
from jobmarket.web.auth import require_owner

EXTRA_ROWS = {"skills": 3, "experiences": 2, "education": 1}

# Messages after a redirect, named by a code in the URL (?n=...), so no free text travels
# in addresses.
NOTICES = {
    "profile_saved": ("good", "Profile saved. Comparisons now use this version."),
    "saved": ("good", "Saved. It stays in Saved jobs even if the listing closes."),
    "unsaved": ("good", "Removed from saved jobs. Its note and any application history are kept."),
    "note_saved": ("good", "Note saved."),
    "note_too_long": ("bad", "Note not saved: keep it under 5,000 characters."),
    "status_saved": ("good", "Status saved."),
    "status_corrected": ("good", "Correction saved; the earlier entry stays in the history."),
    "status_bad_date": ("bad", "Status not saved: enter a valid date."),
    "status_future": ("bad", "Status not saved: the date cannot be in the future."),
    "status_invalid": ("bad", "Status not saved: choose one of the listed statuses."),
    "status_note_too_long": ("bad", "Status not saved: keep the note under 5,000 characters."),
    "status_not_this_job": ("bad", "Not saved: that history entry does not belong to this job."),
    "status_already_corrected": (
        "bad",
        "Not saved: that entry was already corrected; correct the newer one.",
    ),
    "status_nothing_to_correct": ("bad", "Not saved: there is nothing to correct yet."),
}
_TRACKING_CODES = {
    "choose one of the listed statuses": "status_invalid",
    "the date cannot be in the future": "status_future",
    "the date looks wrong": "status_bad_date",
    "keep the note under 5,000 characters": "status_note_too_long",
    "there is nothing to correct yet": "status_nothing_to_correct",
    "that entry does not belong to this job": "status_not_this_job",
    "that entry was already corrected; correct the newer one": "status_already_corrected",
}


def notice() -> tuple[str, str] | None:
    return NOTICES.get(request.args.get("n", ""))


def public_postings(conn, schema: str, posting_ids) -> dict[int, dict]:
    """Public facts about these postings (title, employer, state, links), from the serving
    view."""
    ids = list(posting_ids)
    if not ids:
        return {}
    cur = conn.execute(
        sql.SQL(
            "SELECT posting_id, title, employer_name, source_system, location_text, "
            "availability_state, last_observed_at, first_discovered_at, opportunity_key, "
            "posting_url, apply_url FROM {}.srv_postings WHERE posting_id = ANY(%s)"
        ).format(sql.Identifier(schema)),
        (ids,),
    )
    names = [c.name for c in cur.description]
    return {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}


def register(app, config, conn, private_conn):
    schema = config.private_schema

    def account() -> int:
        return g.owner.account_id

    @app.before_request
    def visit():
        g.visit = None
        if g.get("owner") is not None:
            g.visit = workspace.touch_visit(private_conn(), schema, account(),
                                            g.owner.session_id, g.now)  # fmt: skip

    def back_to_job(posting_id: int, code: str):
        origin = request.form.get("from", "")
        extra = {"from": origin} if origin else {}
        return redirect(url_for("detail", posting_id=posting_id, n=code, **extra))

    # ---------------------------------------------------------------- profile

    @app.route("/profile", methods=["GET", "POST"])
    @require_owner
    def profile():
        g.private_page = True
        saved = workspace.load_profile(private_conn(), schema, account())
        if request.method == "GET":
            rows = workspace.raw_rows(saved)
            return render_template("profile.html", profile=saved, rows=_padded(rows),
                                   errors={}, values=saved)  # fmt: skip
        parsed = workspace.parse_profile_form(request.form, g.now)
        more = request.form.get("more")
        if more in EXTRA_ROWS:  # "add rows" redisplays the form without saving
            rows = _padded(parsed.raw, extra=more)
            return render_template("profile.html", profile=saved, rows=rows, errors={},
                                   values=parsed.profile, confirmed=parsed.confirmed)  # fmt: skip
        if not parsed.ok:
            return render_template("profile.html", profile=saved, rows=_padded(parsed.raw),
                                   errors=parsed.errors, values=parsed.profile,
                                   confirmed=parsed.confirmed), 400  # fmt: skip
        workspace.save_profile(private_conn(), schema, account(), parsed.profile, g.now)
        return redirect(url_for("profile", n="profile_saved"))

    # ------------------------------------------------------------- saved jobs

    @app.get("/saved")
    @require_owner
    def saved_jobs():
        g.private_page = True
        entries = workspace.list_saved(private_conn(), schema, account())
        jobs = public_postings(conn(), config.serving_schema, [e.posting_id for e in entries])
        apps = {a.posting_id: a for a in
                workspace.list_applications(private_conn(), schema, account())}  # fmt: skip
        per_job: dict[str, int] = {}
        for e in entries:
            key = jobs.get(e.posting_id, {}).get("opportunity_key")
            if key:
                per_job[key] = per_job.get(key, 0) + 1
        return render_template("saved.html", entries=entries, jobs=jobs, apps=apps,
                               per_job=per_job)  # fmt: skip

    @app.post("/saved/<int:saved_id>/note")
    @require_owner
    def saved_note(saved_id: int):
        code = "note_saved"
        try:
            changed = workspace.set_saved_note(private_conn(), schema, account(), saved_id,
                                               request.form.get("note", ""))  # fmt: skip
        except ValueError:
            code, changed = "note_too_long", True
        if not changed:
            abort(404)
        posting_id = request.form.get("posting_id", "")
        if posting_id.isdigit():
            return back_to_job(int(posting_id), code)
        return redirect(url_for("saved_jobs", n=code))

    @app.post("/jobs/<int:posting_id>/save")
    @require_owner
    def save(posting_id: int):
        members = search.group_members(conn(), posting_id, config.serving_schema)
        if posting_id not in members or not public_postings(conn(), config.serving_schema,
                                                            [posting_id]):  # fmt: skip
            abort(404)
        workspace.save_job(private_conn(), schema, account(), posting_id, members, g.now)
        return back_to_job(posting_id, "saved")

    @app.post("/jobs/<int:posting_id>/unsave")
    @require_owner
    def unsave(posting_id: int):
        members = search.group_members(conn(), posting_id, config.serving_schema)
        workspace.unsave_job(private_conn(), schema, account(), members, g.now)
        if request.form.get("return") == "saved":
            return redirect(url_for("saved_jobs", n="unsaved"))
        return back_to_job(posting_id, "unsaved")

    # ----------------------------------------------------------- applications

    @app.get("/applications")
    @require_owner
    def applications():
        g.private_page = True
        apps = workspace.list_applications(private_conn(), schema, account())
        jobs = public_postings(conn(), config.serving_schema, [a.posting_id for a in apps])
        return render_template("applications.html", apps=apps, jobs=jobs)

    @app.post("/jobs/<int:posting_id>/status")
    @require_owner
    def status(posting_id: int):
        members = search.group_members(conn(), posting_id, config.serving_schema)
        if not public_postings(conn(), config.serving_schema, [posting_id]):
            abort(404)
        corrects = request.form.get("corrects", "")
        try:
            day = date.fromisoformat(request.form.get("event_date", ""))
        except ValueError:
            return back_to_job(posting_id, "status_bad_date")
        try:
            workspace.record_status(private_conn(), schema, account(), posting_id, members,
                                    request.form.get("status", ""), day,
                                    request.form.get("note", ""), g.now,
                                    int(corrects) if corrects.isdigit() else None)  # fmt: skip
        except workspace.TrackingError as exc:
            return back_to_job(posting_id, _TRACKING_CODES.get(str(exc), "status_invalid"))
        return back_to_job(posting_id, "status_corrected" if corrects else "status_saved")


def _padded(rows: dict[str, list[dict[str, str]]], extra: str | None = None) -> dict:
    """Form rows plus blank ones to type into (more of one kind after 'Add rows')."""
    out = {}
    for kind, blanks in EXTRA_ROWS.items():
        filled = [r for r in rows.get(kind, []) if any(v.strip() for v in r.values())]
        out[kind] = filled + [{} for _ in range(blanks + (blanks if kind == extra else 0))]
    return out


def private_job_context(config, conn, private_conn, posting_id: int) -> dict:
    """The owner's data shown on a job page: saved state, application, and history."""
    members = search.group_members(conn(), posting_id, config.serving_schema)
    schema, account_id = config.private_schema, g.owner.account_id
    saved = workspace.saved_among(private_conn(), schema, account_id, members)
    application = workspace.application_among(private_conn(), schema, account_id, members)
    return {
        "members": members,
        "saved": saved,
        "application": application,
        "today": workspace.today(g.now),
        "statuses": workspace.STATUSES,
    }


def row_marks(config, conn, private_conn, rows) -> dict[str, dict]:
    """Per result (opportunity): whether the owner saved it and its tracked status."""
    keys = [r.opportunity_key for r in rows]
    if not keys:
        return {}
    members: dict[str, list[int]] = {}
    for posting_id, key in conn().execute(
        sql.SQL(
            "SELECT posting_id, opportunity_key FROM {}.srv_postings "
            "WHERE opportunity_key = ANY(%s)"
        ).format(sql.Identifier(config.serving_schema)),
        (keys,),
    ):
        members.setdefault(key, []).append(posting_id)
    schema, account_id = config.private_schema, g.owner.account_id
    all_ids = [i for ids in members.values() for i in ids]
    saved = {s.posting_id for s in workspace.saved_among(private_conn(), schema, account_id,
                                                         all_ids)}  # fmt: skip
    apps = {a.posting_id: a for a in workspace.list_applications(private_conn(), schema,
                                                                 account_id)}  # fmt: skip
    marks = {}
    for key, ids in members.items():
        app = next((apps[i] for i in ids if i in apps), None)
        current = app.current if app else None
        marks[key] = {
            "saved": any(i in saved for i in ids),
            "status": workspace.STATUSES[current.status] if current else None,
        }
    return marks
