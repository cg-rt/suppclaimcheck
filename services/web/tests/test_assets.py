"""Stylesheet URLs carry a hash of the file's content, so a deploy never meets a cached old copy."""

import hashlib

from scc_web.context import STATIC, asset, templates


def test_stylesheets_are_versioned_by_content():
    for name in ("css/app.css", "css/admin.css"):
        digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        assert asset(name) == f"/static/{name}?v={digest}"
    base = templates.get_template("base.html").render(request=None, session=None)
    assert f'href="{asset("css/app.css")}"' in base
