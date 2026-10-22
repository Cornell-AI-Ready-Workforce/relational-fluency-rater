# Provenance: excerpted from relational-fluency@f9947d2.
#   COHORTS, known_cohorts, normalize_cohort        <- server/runs.py:58-85
#   _RUN_ID_RE, _PARTICIPANT_KEY_RE,
#   _PARTICIPANT_KEY_PLACEHOLDERS,
#   normalize_participant_key                       <- server/runs.py:488-532
#   SESSION_ID_RE, valid_session_id                 <- server/storage.py:117,128-130
# Copied as excerpts, not whole modules: runs.py imports scenarios_v3 and
# storage, and taking it whole would fork the step 1 app. tests/test_parity.py
# compares every name above, by AST, with the old repository when it is checked
# out beside this one, and pins the behaviour with golden vectors everywhere.
# Change them there first, then re-copy; never only here.
# --- end provenance ---
"""The participant, cohort and session-id conventions of step 1, shared.

This app reads data step 1 wrote and writes records that point back at it, so
a participant key, a cohort name or a session id must mean exactly what it
means in relational-fluency: the same string accepted, the same one refused,
the same refusal reason. Everything here is copied, not re-derived.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# The cohorts a run may be in. Every consumer — ?cohort= on /api/runs and
# /api/sessions, the rating split, the analysis set — compares with ==, so a
# cohort that is not one of these three is not "a different group", it is a run
# that no filter can name: it falls out of ?cohort=study AND ?cohort=internal at
# once, so it is neither excluded from the dataset nor present in it. Round two
# gated who may set the parameter and left the string alone, and 'Internal',
# 'INTERNAL', ' internal ' and a newline-bearing one all landed on runs.
COHORTS: Tuple[str, ...] = ("study", "internal", "unattributed")


def known_cohorts() -> Tuple[str, ...]:
    """The cohort names a run may carry."""
    return COHORTS


def normalize_cohort(raw: Optional[str]) -> str:
    """The canonical spelling of a cohort name.

    Raises ValueError for anything else, which is the same refusal an unknown
    arm and an unknown variant letter already get, and for the same reason: a
    link that cannot build the run it claims to must fail where it is handed
    out. Storing 'banana' does not produce a differently-grouped run, it
    produces an invisible one, and nobody finds out until an analyst counts the
    wave and it is short.

    None and empty mean "not stated" and become the default, "study" — which is
    what every caller that omits the argument already meant.
    """
    s = " ".join(str(raw or "").split()).strip().lower()
    if not s:
        return "study"
    if s not in COHORTS:
        raise ValueError(
            f"unknown cohort: {raw!r} (a run may be in {', '.join(COHORTS)})")
    return s


_RUN_ID_RE = re.compile(r"[0-9a-f]{12}")

# A usable participant key: the CloudResearch/Prolific-style identifier Qualtrics
# pipes into /start. Deliberately narrow, everything a working pipe produces is
# alphanumeric with at most - and _, and everything a broken pipe produces
# (empty, "${e://Field/ParticipantKey}", a stray URL, a sentence) is not.
_PARTICIPANT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")

# Values that are shaped like a key but are plainly not one: the name of the
# embedded field, or a placeholder the survey never filled in. Accepting one of
# these is worse than rejecting it, because every participant sends the SAME
# string, so find_for_participant hands the second arrival the first person's
# half-finished run.
_PARTICIPANT_KEY_PLACEHOLDERS = {
    "participantkey", "participant_key", "participantid", "participant_id",
    "prolificpid", "prolific_pid", "responseid", "response_id", "workerid",
    "worker_id", "embedded", "undefined", "unknown", "null", "none", "empty",
    "xxxxxx", "xxxxxxxx",
}


def normalize_participant_key(raw: Optional[str]) -> Tuple[Optional[str], str]:
    """Return (key, status) for a participant key arriving from the survey.

    status is "ok" with the cleaned key, or one of "missing"/"unpiped"/
    "placeholder"/"malformed" with key None. Qualtrics piping fails in known
    ways (see docs/OPERATIONS.md, which even tells the operator to guess the
    embedded field name), and both failure modes corrupt the data silently: an
    undefined field renders as the empty string, so every arrival forks a fresh
    unattributable run; an unreplaced field renders as the literal
    "${e://Field/ParticipantKey}", so every participant collides into one run.
    Callers must decide what to do with a bad key, but must never take it at
    face value.
    """
    key = (raw or "").strip()
    if not key:
        return None, "missing"
    # Unreplaced Qualtrics piping, in any of the spellings it leaks in as.
    if "${" in key or "e://" in key or "}" in key:
        return None, "unpiped"
    if key.lower() in _PARTICIPANT_KEY_PLACEHOLDERS:
        return None, "placeholder"
    if not _PARTICIPANT_KEY_RE.fullmatch(key):
        return None, "malformed"
    return key, "ok"


# Session ids are minted by session.new_session_id as f"s_{epoch}_{token_hex(3)}".
# raters.py has enforced exactly this for a while; it lives here now so app.py,
# rater_packet.py and video.py can share the one definition instead of the
# permissive [A-Za-z0-9_-]{1,64} they each carried, which accepted uppercase.
SESSION_ID_RE = re.compile(r"s_[0-9]{1,20}_[0-9a-f]{6}")


def valid_session_id(session_id: Optional[str]) -> bool:
    """True when this is exactly the shape session.new_session_id mints."""
    return bool(session_id) and SESSION_ID_RE.fullmatch(session_id) is not None


# --- this app's own, not copied ---------------------------------------------------
#
# What a stored record may name as the step 1 participant. Not the same rule as
# normalize_participant_key: that one judges a key arriving from a survey, while
# this judges a key step 1 already wrote, which also includes the synthetic
# `unattributed_<hex>` and `test_<name>_<epoch>` identities step 1 mints for
# arrivals whose own key was unusable. Narrow enough to be a safe S3 key
# component and a safe log field; wide enough for every identity step 1 issues.
_STORED_PARTICIPANT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def valid_run_id(run_id: Optional[str]) -> bool:
    """True when this is exactly the shape step 1 mints run ids in."""
    return bool(run_id) and _RUN_ID_RE.fullmatch(run_id) is not None


def valid_stored_participant_key(key: Optional[str]) -> bool:
    return bool(key) and _STORED_PARTICIPANT_KEY_RE.fullmatch(key) is not None
