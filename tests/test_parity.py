"""Parity with relational-fluency (step 1).

This app reads what step 1 wrote and writes records that point back at it, so a
participant key, a cohort, a session id or an assigned identity must mean
exactly what it means there. Two layers:

* golden vectors, which always run and pin the behaviour itself;
* a comparison with step 1's source, which runs when that repository is checked
  out beside this one (or at RF_STEP1_REPO) and skips with that reason when it
  is not. When step 1 changes one of these, this fails: re-copy, do not patch.
"""
from __future__ import annotations

import ast
import hashlib
import re

import pytest

from conftest import OLD_REPO, ROOT, old_file
from server import conventions as cv
from server import identity

END = "# --- end provenance ---\n"


# --- golden vectors -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("abc123", ("abc123", "ok")),
    ("  R_3kXyz123  ", ("R_3kXyz123", "ok")),
    ("a" * 64, ("a" * 64, "ok")),
    ("a" * 65, (None, "malformed")),
    ("abc12", (None, "malformed")),
    ("abc 123", (None, "malformed")),
    ("abc.123", (None, "malformed")),
    ("", (None, "missing")),
    (None, (None, "missing")),
    ("   ", (None, "missing")),
    ("${e://Field/ParticipantKey}", (None, "unpiped")),
    ("e://Field/x", (None, "unpiped")),
    ("abc}123", (None, "unpiped")),
    ("ParticipantKey", (None, "placeholder")),
    ("UNDEFINED", (None, "placeholder")),
    ("xxxxxx", (None, "placeholder")),
])
def test_normalize_participant_key_vectors(raw, expected):
    assert cv.normalize_participant_key(raw) == expected


def test_cohort_vectors():
    assert cv.COHORTS == ("study", "internal", "unattributed")
    assert cv.known_cohorts() == cv.COHORTS
    assert cv.normalize_cohort(None) == "study"
    assert cv.normalize_cohort("") == "study"
    assert cv.normalize_cohort(" Internal\n") == "internal"
    assert cv.normalize_cohort("UNATTRIBUTED") == "unattributed"
    with pytest.raises(ValueError):
        cv.normalize_cohort("pilot")


@pytest.mark.parametrize("sid,ok", [
    ("s_1727000000_a1b2c3", True),
    ("s_1_000000", True),
    ("s_1727000000_A1B2C3", False),   # minted lowercase; S3 keys are case-sensitive
    ("s_1727000000_a1b2c", False),
    ("s__a1b2c3", False),
    ("s_1727000000_a1b2c3/..", False),
    ("", False),
    (None, False),
])
def test_session_id_vectors(sid, ok):
    assert cv.valid_session_id(sid) is ok


def test_session_id_regex_is_steps_exactly():
    assert cv.SESSION_ID_RE.pattern == r"s_[0-9]{1,20}_[0-9a-f]{6}"


def test_run_id_and_stored_key():
    assert cv.valid_run_id("0123456789ab")
    assert not cv.valid_run_id("0123456789AB")
    assert not cv.valid_run_id("0123456789a")
    for k in ("abc123", "unattributed_0a1b2c3d4e5f", "test_tanvi_1727000000"):
        assert cv.valid_stored_participant_key(k)
    for k in ("", None, "a/b", "a b", "x" * 129):
        assert not cv.valid_stored_participant_key(k)


# Computed with relational-fluency@f9947d2:server/identity.py. Step 1 calls
# assign() with the participant RECORD id (p_<n>_<hex>; server/session.py:80),
# not with the survey key, so that is the input a caller here must use too.
@pytest.mark.parametrize("key,construct,name,role", [
    ("p_1727000000_a1b2c3", "conflict_management", "Tatum", "senior analyst"),
    ("p_1_000000", "influence", "Reese", "senior analyst"),
    ("R_3kXyz123", "inspirational_leadership", "Peyton", "interim team lead"),
    ("", "teamwork", "Reese", "project coordinator"),
    ("selftest1", "unknown_construct", "Reese", "team member"),
])
def test_identity_assign_vectors(key, construct, name, role):
    assert identity.assign(key, construct) == {
        "name": name, "role": role, "org": "a mid-sized company"}


# --- source comparison with step 1 ------------------------------------------------------

@pytest.mark.parametrize("new,old", [
    ("server/build_info.py", "server/build_info.py"),
    ("server/identity.py", "server/identity.py"),
    ("tools/pinned_image.py", "tools/pinned_image.py"),
])
def test_verbatim_copies_match_step1(new, old):
    text = (ROOT / new).read_text(encoding="utf-8")
    assert END in text, f"{new} has lost its provenance header"
    header, body = text.split(END, 1)
    assert "relational-fluency@" in header and old in header
    assert body == old_file(old).read_text(encoding="utf-8"), (
        f"{new} no longer matches step 1's {old}: re-copy it (and its provenance commit)")


def _named_nodes(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            out[node.name] = node
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for t in targets:
            if t in names:
                out[t] = node
    return {k: ast.dump(v, include_attributes=False) for k, v in out.items()}


EXCERPTS = {
    "server/runs.py": ["COHORTS", "known_cohorts", "normalize_cohort", "_RUN_ID_RE",
                       "_PARTICIPANT_KEY_RE", "_PARTICIPANT_KEY_PLACEHOLDERS",
                       "normalize_participant_key"],
    "server/storage.py": ["SESSION_ID_RE", "valid_session_id"],
}


@pytest.mark.parametrize("old", sorted(EXCERPTS))
def test_conventions_excerpts_match_step1(old):
    names = EXCERPTS[old]
    theirs = _named_nodes(old_file(old), names)
    ours = _named_nodes(ROOT / "server/conventions.py", names)
    assert set(theirs) == set(names), f"step 1's {old} no longer defines {set(names) - set(theirs)}"
    for n in names:
        assert ours.get(n) == theirs[n], f"conventions.{n} differs from step 1's {old}: re-copy it"


def test_shared_requirement_pins_match_step1():
    def pins(text):
        return dict(re.findall(r"(?m)^([A-Za-z0-9_.\-\[\]]+)==(\S+)", text))
    ours = pins((ROOT / "requirements.txt").read_text())
    theirs = pins(old_file("requirements.txt").read_text())
    shared = set(ours) & set(theirs)
    assert {"fastapi", "boto3", "botocore", "uvicorn[standard]"} <= shared
    assert {k: ours[k] for k in shared} == {k: theirs[k] for k in shared}
    assert set(ours) <= set(theirs), "a package here that step 1 does not pin: pin it there first"


# SHA-256 of step 1's files at f9947d2, so the check runs without it too.
ASSETS = {
    "static/fonts/inter-latin-wght-normal.woff2":
        "3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62",
    "static/fonts/LICENSE-Inter.txt":
        "3b0a5fca3d17942cde889069889dedbbbd075e9b599968c82a95f4d944e9b345",
    "static/logo.png":
        "d21e09e27ad8592a6755e34cff3a5d1a61e7a46509599530f91d41423eab74d7",
    "static/favicon.png":
        "9cd74d5e0fd7566cd19e7cd586d48fb1076dec8c71339c5445ecdc66fda338d3",
    "static/apple-touch-icon.png":
        "b8fe4c631ae7bebec20a63f11844311c953bb589599bf745e8858b2cd289e891",
}


@pytest.mark.parametrize("rel", sorted(ASSETS))
def test_assets_are_unchanged_copies(rel):
    assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == ASSETS[rel]


@pytest.mark.parametrize("rel", sorted(ASSETS))
def test_assets_are_byte_identical_to_step1(rel):
    ours = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
    theirs = hashlib.sha256(old_file(rel).read_bytes()).hexdigest()
    assert ours == theirs, f"{rel} differs from step 1's copy"

