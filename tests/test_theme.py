"""One theme, step 1's, on every page, and nothing fetched from elsewhere."""
from __future__ import annotations

import re

import pytest

from conftest import ROOT, old_file

STATIC = ROOT / "static"
PAGES = sorted(STATIC.glob("*.html"))
THEME = (STATIC / "theme.css").read_text(encoding="utf-8")


def _block(text: str, opener: str) -> str:
    i = text.index(opener)
    return text[i:text.index("}", i) + 1]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def test_there_are_pages():
    assert {p.name for p in PAGES} >= {"landing.html", "start.html", "visit.html"}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_page_uses_theme_and_icons(page):
    html = page.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/theme.css" />' in html
    assert 'href="/static/favicon.png"' in html
    assert 'href="/static/apple-touch-icon.png"' in html
    assert '<html lang="en">' in html
    # Tokens live in theme.css only, so a page cannot drift from the palette.
    assert ":root" not in html
    assert html.count("<h1") == 1


@pytest.mark.parametrize("path", PAGES + [STATIC / "theme.css"], ids=lambda p: p.name)
def test_no_third_party_requests(path):
    text = path.read_text(encoding="utf-8")
    refs = re.findall(r"""(?:src|href|action)\s*=\s*["']([^"']+)""", text)
    refs += re.findall(r"""url\(\s*["']?([^"')]+)""", text)
    refs += re.findall(r"""@import\s+["']([^"']+)""", text)
    for ref in refs:
        assert ref.startswith("/") and not ref.startswith("//"), f"{path.name}: {ref}"
    assert "<script src" not in text


def test_font_is_self_hosted_and_preloaded():
    assert 'src: url("/static/fonts/inter-latin-wght-normal.woff2") format("woff2")' in THEME
    for page in PAGES:
        assert '<link rel="preload" href="/static/fonts/inter-latin-wght-normal.woff2"' in page.read_text()


def test_controls_use_line_strong_edges():
    # Step 1's accessibility choice: a control's edge is >= 3:1 (--line-strong),
    # not --line (1.68:1).
    for sel in ("button.secondary {", "select {", ".card .code-row input {"):
        rule = THEME[THEME.index(sel):THEME.index("}", THEME.index(sel))]
        assert "var(--line-strong)" in rule, sel
    assert "outline: 2px solid var(--fg)" in THEME
    assert "prefers-reduced-motion: reduce" in THEME
    assert "min-height: 44px" in THEME


def test_tokens_and_font_face_match_step1_v2():
    v2 = old_file("static/v2.html").read_text(encoding="utf-8")
    assert _norm(_block(THEME, ":root {")) == _norm(_block(v2, ":root {"))
    assert _norm(_block(THEME, "@font-face {")) == _norm(_block(v2, "@font-face {"))
