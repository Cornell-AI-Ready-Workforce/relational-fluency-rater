"""The rating screen (static/rater/rate.html). It keeps the UI prototype's own
design rather than theme.css, so test_theme.py does not cover it; it still
fetches nothing from another site and uses only routes the app serves."""
from __future__ import annotations

import re

from conftest import ROOT
from server import app as appmod

PAGE = ROOT / "static" / "rater" / "rate.html"
TEXT = PAGE.read_text(encoding="utf-8")


def test_is_the_page_rate_serves():
    assert appmod.RATE_PAGE == PAGE
    assert '<html lang="en">' in TEXT and 'name="robots" content="noindex, nofollow"' in TEXT


def test_no_third_party_requests():
    refs = re.findall(r"""(?:src|href|action)\s*=\s*["']([^"'$]+)""", TEXT)
    refs += re.findall(r"""url\(\s*["']?([^"')]+)""", TEXT)
    assert refs
    for ref in refs:
        assert ref.startswith(("/", "#")) and not ref.startswith("//"), ref
    assert "<script src" not in TEXT and "googleapis" not in TEXT and "http://" not in TEXT
    assert "https://" not in TEXT


def test_calls_only_the_rater_api():
    calls = set(re.findall(r"""['"`](/(?:api|rate)[^'"`$]*)""", TEXT))
    assert calls == {"/api/batch", "/api/rate/", "/rate"}, calls
    paths = {r.path for r in appmod.app.routes}
    assert {"/api/batch", "/api/rate/{code}", "/api/rate/{code}/draft", "/api/rate/{code}/submit",
            "/rate/{code}/media", "/rate"} <= paths


def test_shows_the_build_tag_bug_reports_quote():
    """The bug form (.github/ISSUE_TEMPLATE/bug_report.yml) asks for it."""
    assert TEXT.count('<div class="build-tag">{{build}}</div>') == 1
