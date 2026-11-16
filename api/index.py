"""Vercel entry point: exposes the Flask WSGI app from the project root."""
import os
import sys
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  (Vercel looks for a WSGI variable named "app")

PATH_PARAM = "__path"


class _RestoreOriginalPath:
    """vercel.json rewrites every request to /api/index?__path=<original path>.

    Vercel hands the function the rewritten path (/api/index), so without this
    every Flask route 404s. Put the original path back and drop the helper param.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        original = None
        rest = []
        for key, value in pairs:
            if key == PATH_PARAM and original is None:
                original = value
            else:
                rest.append((key, value))
        if original is not None:
            # WSGI PATH_INFO is UTF-8 bytes decoded as latin-1.
            environ["PATH_INFO"] = ("/" + original.lstrip("/")).encode("utf-8").decode("latin-1")
            environ["SCRIPT_NAME"] = ""
            environ["QUERY_STRING"] = urlencode(rest)
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _RestoreOriginalPath(app.wsgi_app)
