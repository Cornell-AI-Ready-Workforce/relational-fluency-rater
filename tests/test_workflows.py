"""The build workflow builds what the stack deploys, the way step 1 builds."""
import re

from conftest import ROOT

WF = ROOT / ".github" / "workflows"


def test_build_image_uses_oidc_and_this_repo():
    t = (WF / "build-image.yml").read_text()
    assert "id-token: write" in t and "role-to-assume: ${{ secrets.AWS_ROLE_ARN }}" in t
    assert re.search(r"(?m)^on:\n  workflow_dispatch:\n\n", t), "manual only, like step 1's"
    assert "/relational-fluency-rater/app" in t and "relational-fluency/platform" not in t
    assert 'docker build --platform linux/amd64 --build-arg BUILD_SHA="$SHA" -t "$REPO:$SHA" .' in t
    assert "SHA=${GITHUB_SHA::7}" in t and "*ImageNotFoundException*" in t


def test_ci_runs_matrix_and_tofu_without_credentials():
    t = (WF / "ci.yml").read_text()
    assert '["3.11", "3.12", "3.13"]' in t
    assert "init -backend=false" in t and "secrets." not in t
    assert "repository: Cornell-AI-Ready-Workforce/relational-fluency\n" in t
