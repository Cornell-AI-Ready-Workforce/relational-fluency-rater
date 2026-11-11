"""The bug form asks for what places a report against a deploy, and warns first.

Read as text: this app reads no YAML and does not depend on PyYAML.
"""
import re

from conftest import ROOT

FORM = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
CONFIG = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text()


def _fields():
    """id -> the text of that field's block, up to the next field."""
    blocks = re.split(r"(?m)^  - type: ", FORM)[1:]
    out = {}
    for b in blocks:
        m = re.search(r"(?m)^    id: (\w+)$", b)
        if m:
            out[m.group(1)] = b
    return out


def test_required_fields():
    fields = _fields()
    for fid in ("build", "page", "rating_code", "environment", "when", "what_happened", "expected"):
        assert "required: true" in fields[fid], fid
    assert "required: false" in fields["more"]


def test_warns_about_public_repo_and_secrets_in_links():
    intro = re.split(r"(?m)^  - type: ", FORM)[1]
    assert "markdown" in intro.splitlines()[0]
    assert "public" in intro and "token=" in intro and "key=" in intro


def test_blank_issues_stay_enabled():
    assert re.search(r"(?m)^blank_issues_enabled: true$", CONFIG)
