"""The Dockerfile and .dockerignore agree, and the image keeps step 1's
non-root uid 1000 and BUILD_SHA plumbing."""
import re

from conftest import ROOT


def test_dockerignore_allowlist_matches_copy_lines():
    docker = (ROOT / "Dockerfile").read_text()
    copied = set(re.findall(r"(?m)^COPY\s+(\S+)\s", docker))
    allowed = {l[1:] for l in (ROOT / ".dockerignore").read_text().splitlines() if l.startswith("!")}
    assert copied == allowed == {"requirements.txt", "server", "static"}


def test_non_root_uid_1000_and_build_sha():
    docker = (ROOT / "Dockerfile").read_text()
    assert "useradd -m -u 1000 appuser" in docker
    assert re.search(r"(?m)^USER appuser$", docker)
    assert 'ARG BUILD_SHA=""' in docker and "ENV BUILD_SHA=$BUILD_SHA" in docker
    assert re.search(r"(?m)^FROM python:3\.12-slim$", docker)
    assert 'CMD ["python", "-m", "server.app"]' in docker


def test_requirements_are_exact_pins():
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[0-9][0-9A-Za-z.]*", line), line
