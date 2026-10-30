"""Signing in and out, and the checks every private page and form goes through.

- The session cookie carries only a random token (HttpOnly, SameSite=Strict, Secure with the
  __Host- prefix by default); the session itself is in the private schema (jobmarket.owner).
- Every POST is checked twice: its Origin header (when sent) must be this site, and its form
  must carry the session's CSRF token. The sign-in form, which has no session yet, uses a
  separate random cookie and matching form field instead.
- Private pages and forms call require_owner(); without a valid session they redirect to sign
  in (GET) or are refused (POST), whatever ID the URL names.
- Responses for a signed-in owner, and every private page, are sent with
  Cache-Control: no-store; signing out also sends Clear-Site-Data.
"""

import hmac
import math
import secrets
from functools import wraps
from urllib.parse import urlsplit

import psycopg
from flask import abort, g, redirect, render_template, request, url_for

from jobmarket import owner

LOGIN_CSRF_COOKIE = "jm_signin"


def cookie_name(secure: bool) -> str:
    return "__Host-jm_session" if secure else "jm_session"


def safe_next(target: str | None) -> str:
    """A local path to return to after signing in (never another site)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


def same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # some browsers omit it on same-origin form posts; the token still applies
    return origin.rstrip("/") == request.host_url.rstrip("/")


def require_owner(view):
    """Private pages and forms: the session is checked on the server for every request."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get("owner") is None:
            if request.method == "GET":
                return redirect(url_for("sign_in", next=request.full_path.rstrip("?")))
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def register(app, config, private_conn):
    """Add the sign-in pages and the per-request session and CSRF checks to the app."""
    name = cookie_name(config.cookie_secure)

    def set_cookie(response, key, value, expires=None, max_age=None):
        response.set_cookie(key, value, expires=expires, max_age=max_age, path="/",
                            secure=config.cookie_secure, httponly=True,
                            samesite="Strict")  # fmt: skip

    @app.before_request
    def load_owner():
        g.owner = None
        g.private_error = None
        token = request.cookies.get(name)
        if token and config.private_settings is not None:
            try:
                g.owner = owner.load_session(private_conn(), config.private_schema, token, g.now)
            except psycopg.OperationalError:
                g.private_error = "unreachable"
        if request.method == "POST":
            if not same_origin():
                abort(403)
            # Signing out without a valid session only clears the cookie, so it needs no token.
            exempt = request.endpoint == "sign_in" or (
                request.endpoint == "sign_out" and g.owner is None
            )
            if not exempt:
                sent = request.form.get("csrf_token", "")
                if g.owner is None or not hmac.compare_digest(sent, g.owner.csrf_token):
                    abort(403)

    @app.after_request
    def no_store(response):
        response.headers.add("Vary", "Cookie")
        if g.get("owner") is not None or g.get("private_page"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def owner_context():
        return {"owner": g.get("owner")}

    def private_unavailable():
        if config.private_settings is None:
            return ("Private pages are not set up on this computer: PRIVATE_DB_PASSWORD is "
                    "missing from .env.")  # fmt: skip
        if g.get("private_error"):
            return "The private database login could not connect."
        return None

    @app.route("/signin", methods=["GET", "POST"])
    def sign_in():
        g.private_page = True
        target = safe_next(request.values.get("next"))
        problem = private_unavailable()
        error = None
        if request.method == "POST" and problem is None:
            sent = request.form.get("signin_token", "")
            cookie = request.cookies.get(LOGIN_CSRF_COOKIE, "")
            if not cookie or not hmac.compare_digest(sent, cookie):
                error = "This sign-in form expired. Please try again."
            else:
                try:
                    started = owner.sign_in(private_conn(), config.private_schema,
                                            request.form.get("username", ""),
                                            request.form.get("password", ""), g.now,
                                            request.remote_addr)  # fmt: skip
                except owner.SignInPaused as paused:
                    minutes = max(1, math.ceil((paused.until - g.now).total_seconds() / 60))
                    error = (f"Too many wrong passwords. Sign-in is paused; try again in "
                             f"{minutes} minute{'s' * (minutes != 1)}.")  # fmt: skip
                else:
                    if started is None:
                        error = "The username or password is not correct."
                    else:
                        token, session = started
                        old = request.cookies.get(name)
                        if old:  # a previous session in this browser ends now
                            stale = owner.load_session(private_conn(), config.private_schema,
                                                       old, g.now)  # fmt: skip
                            if stale and stale.session_id != session.session_id:
                                owner.end_session(private_conn(), config.private_schema,
                                                  stale.session_id)  # fmt: skip
                        response = redirect(target)
                        set_cookie(response, name, token, expires=session.expires_at)
                        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
                        return response
        form_token = secrets.token_urlsafe(32)
        response = app.make_response((render_template(
            "signin.html", next=target, problem=problem, error=error, form_token=form_token,
            username=request.form.get("username", "") if request.method == "POST" else "",
        ), 401 if error else 200))  # fmt: skip
        set_cookie(response, LOGIN_CSRF_COOKIE, form_token, max_age=1800)
        return response

    @app.post("/signout")
    def sign_out():
        g.private_page = True
        if g.owner is not None:
            owner.end_session(private_conn(), config.private_schema, g.owner.session_id)
        response = redirect(url_for("sign_in", signed_out=1))
        response.delete_cookie(name, path="/", secure=config.cookie_secure, httponly=True,
                               samesite="Strict")  # fmt: skip
        response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
        g.owner = None
        return response

    @app.get("/account")
    @require_owner
    def account():
        g.private_page = True
        return render_template("account.html")
