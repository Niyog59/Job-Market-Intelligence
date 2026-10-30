"""The job-search website: search results and job detail pages, server-rendered with Flask
templates and a small script for tabs, evidence, and scroll restoration, plus the owner's
private pages after signing in (jobmarket.web.auth).

Public pages only read: they use the read-only analytics login in read-only sessions.
Private pages use the private login, which can reach only the private schema. Browsing never
calls a source, the model, or dbt. The site is meant to run locally (`jobmarket web`, bound
to 127.0.0.1).
"""

from urllib.parse import parse_qsl

import psycopg
from flask import Flask, abort, g, render_template, request
from werkzeug.datastructures import MultiDict

from jobmarket import db, search, workspace
from jobmarket.web import auth, private_pages
from jobmarket.web import format as fmt
from jobmarket.web.config import WebConfig, load_web_config
from jobmarket.web.pages import job_page

# Strict policy: everything (scripts, styles, fonts) is served by the site itself.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def create_app(config: WebConfig | None = None) -> Flask:
    config = config or load_web_config()
    app = Flask(__name__)
    app.config["WEB"] = config

    def conn() -> psycopg.Connection:
        if "conn" not in g:
            options = {"application_name": "jobmarket-web"}
            if config.source_schema:
                options["options"] = f"-c search_path={config.source_schema}"
            c = db.connect(config.settings, **options)
            c.read_only = True
            g.conn = c
        return g.conn

    def private_conn() -> psycopg.Connection:
        if "private_conn" not in g:
            g.private_conn = db.connect(
                config.private_settings, autocommit=True, application_name="jobmarket-private"
            )
        return g.private_conn

    # The request's connections (tests use them).
    app.extensions["jobmarket_db"] = conn
    app.extensions["jobmarket_private_db"] = private_conn

    @app.teardown_appcontext
    def close(_exc):
        for key in ("conn", "private_conn"):
            c = g.pop(key, None)
            if c is not None:
                c.close()

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        # same-origin: the site's own form posts carry their Origin (with no-referrer, browsers
        # send "Origin: null" and the CSRF origin check would refuse them); other sites still
        # receive no referrer, and external links also carry rel="noreferrer".
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.context_processor
    def helpers():
        now = g.get("now") or config.clock()
        return {
            "fmt": fmt,
            "now": now,
            "S": search,
        }

    def results_for(args) -> tuple[search.SearchResult, search.Choices]:
        choices = search.load_choices(conn(), config.serving_schema)
        params, problems = search.parse_params(args, choices)
        visit = g.get("visit")
        cutoff = visit.cutoff if visit is not None else None
        result = search.search(conn(), params, g.now, problems, config.serving_schema,
                               new_cutoff=cutoff)  # fmt: skip
        return result, choices

    @app.before_request
    def stamp():
        g.now = config.clock()

    # After stamp(): the session check needs the time; visits need the session.
    auth.register(app, config, private_conn)
    private_pages.register(app, config, conn, private_conn)

    @app.context_processor
    def notices():
        return {
            "notice": private_pages.notice(),
            "statuses": workspace.STATUSES,
            "kinds": workspace.EXPERIENCE_KINDS,
            "levels": workspace.EDUCATION_LEVELS,
            "edu_status": workspace.EDUCATION_STATUS,
        }

    @app.get("/")
    def results():
        result, choices = results_for(request.args)
        status = search.site_status(conn(), g.now, config.serving_schema)
        popular = search.popular_skills(conn(), result.params.population, config.serving_schema)
        marks = (private_pages.row_marks(config, conn, private_conn, result.rows)
                 if g.get("owner") else {})  # fmt: skip
        return render_template("results.html", result=result, choices=choices, status=status,
                               popular=popular, marks=marks, visit=g.get("visit"))  # fmt: skip

    @app.get("/jobs/<int:posting_id>")
    def detail(posting_id: int):
        page = job_page(conn(), posting_id, config.serving_schema)
        if page is None:
            abort(404)
        # The search the reader came from (only a query string; the path is always "/").
        back = MultiDict(parse_qsl(request.args.get("from", ""), keep_blank_values=False))
        browse, _ = results_for(back)
        mine = (private_pages.private_job_context(config, conn, private_conn, posting_id, page)
                if g.get("owner") else None)  # fmt: skip
        return render_template("detail.html", page=page, browse=browse, back=browse.params,
                               mine=mine)  # fmt: skip

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", title="Job not found",
                               message="This link does not match a listing with stored "
                               "content. It may be mistyped, or the listing was never "
                               "stored in full."), 404  # fmt: skip

    @app.errorhandler(search.ServingUnavailable)
    def unavailable(exc):
        return render_template(
            "error.html",
            title="Search data not available",
            message=f"The website cannot read its data: {exc}.",
        ), 503

    @app.errorhandler(psycopg.OperationalError)
    def database_down(_exc):
        return render_template("error.html", title="Database not reachable",
                               message="The website could not connect to the database. "
                               "Check that PostgreSQL is running and that ANALYTICS_DB_PASSWORD "
                               "is set in .env."), 503  # fmt: skip

    @app.errorhandler(psycopg.errors.UndefinedTable)
    def rebuilding(_exc):
        return render_template("error.html", title="Search data is being rebuilt",
                               message="The job-search views are missing or being rebuilt. "
                               "Run `jobmarket analytics build` and reload."), 503  # fmt: skip

    return app
