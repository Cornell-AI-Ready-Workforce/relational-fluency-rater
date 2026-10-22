"""Shared test setup.

OLD_REPO is relational-fluency (step 1), checked out beside this repository.
Tests that compare against it skip, with that reason, when it is not there;
the golden vectors in the same files still pin the behaviour.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OLD_REPO = Path(os.environ.get("RF_STEP1_REPO", ROOT.parent / "relational-fluency")).resolve()


def old_file(rel: str) -> Path:
    p = OLD_REPO / rel
    if not p.is_file():
        pytest.skip(f"step 1 repository not checked out at {OLD_REPO} "
                    f"(set RF_STEP1_REPO); cannot compare {rel}")
    return p


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No test reads or writes a real bucket: both stores go to tmp dirs."""
    monkeypatch.delenv("SOURCE_BUCKET", raising=False)
    monkeypatch.delenv("RATER_BUCKET", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BUILD_SHA", raising=False)
    yield
