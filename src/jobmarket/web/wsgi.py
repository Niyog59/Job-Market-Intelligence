"""WSGI entry point for a production server: ``gunicorn jobmarket.web.wsgi:app``.

Configuration comes from the environment, as for ``jobmarket web``.

WEB_TRUST_PROXY=1 is for running behind exactly one reverse proxy (Caddy in deploy/). The app
then takes the client address, scheme, and host from that proxy's X-Forwarded-For,
X-Forwarded-Proto, and X-Forwarded-Host headers, so the sign-in origin check compares against
https://<site> and the login records the visitor's address rather than the proxy's. Leave it
unset when clients can reach the app directly, or they could fake those headers.
"""

import os

from werkzeug.middleware.proxy_fix import ProxyFix

from jobmarket.web import create_app

app = create_app()

if os.environ.get("WEB_TRUST_PROXY", "").strip() == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
