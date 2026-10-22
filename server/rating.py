"""Raters, their queue, and the ratings they submit. Logic only; routes in app.py.

WHO RATES. Trained team raters, not participants (decided 2026-10-22, after the
"Relational Fluency Annotation" prototype). A rater is created by a researcher
(POST /api/raters) and gets a one-time link carrying a token; opening it sets a
signed, HttpOnly cookie and the token leaves the address bar. Everything after
that is keyed by the cookie.

BLIND BY CONSTRUCTION. A rater must not learn which participant, scenario or
competency they are rating. So every encounter a rater is given is named by a
RATING CODE, `RC-` and ten hex digits derived from the rater and the step 1
session id, and nothing the rater-facing routes return carries a session id,
participant key, run id, cohort, scenario, model or character name; the
transcript shows "Participant" and "Character 1..n". tests/test_rating.py
greps every rater-facing response for those shapes.

WHERE IT GOES. This app's bucket, under prefixes server/store.py lists and
infra/terraform mirrors (retention and IAM):

    raters/<rater_id>.json                 who, and the SHA-256 of their token
    assignments/<rater_id>/<code>.json     which encounter the code stands for
    drafts/<rater_id>/<code>.json          autosaved answers, overwritten freely
    ratings/<rater_id>/<code>.json         the submission, written once

Assignments and ratings are records in the store's sense: they carry the step 1
references (participant_key, session_ids, run_id) so the researcher side can
join them, and the rater side never sees that block.
"""
from __future__ import annotations

import hashlib
import hmac
import random
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from . import items as _items
from .conventions import valid_session_id
from .store import RecordStore, StoreError, new_record

RATER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}")
CODE_RE = re.compile(r"RC-[0-9A-F]{10}")
SESSION_SECONDS = 12 * 3600
COOKIE = "rf_rater"


class RatingError(ValueError):
    pass


def _now() -> float:
    return time.time()


# --- raters -------------------------------------------------------------------------

def rater_key(rater_id: str) -> str:
    return f"raters/{rater_id}.json"


def valid_rater_id(rater_id: Optional[str]) -> bool:
    return bool(rater_id) and RATER_ID_RE.fullmatch(rater_id) is not None


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_rater(store: RecordStore, rater_id: str, display_name: str = "") -> Tuple[Dict[str, Any], str]:
    """Create a rater; returns (record, token). The token is returned exactly
    once and stored only as its digest."""
    if not valid_rater_id(rater_id):
        raise RatingError("rater_id must be 2-32 letters, digits, - or _")
    token = secrets.token_urlsafe(24)
    rec = {"kind": "rater", "schema": 1, "rater_id": rater_id,
           "display_name": (display_name or "").strip()[:80],
           "token_sha256": _token_digest(token), "active": True,
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not store.put(rater_key(rater_id), rec, create_only=True):
        raise RatingError(f"rater {rater_id!r} already exists")
    return rec, token


def list_raters(store: RecordStore) -> List[Dict[str, Any]]:
    out = []
    for k in store.list_keys("raters/"):
        r = store.get(k) or {}
        if r.get("kind") == "rater":
            out.append(r)
    return sorted(out, key=lambda r: r.get("rater_id") or "")


def public_rater(r: Dict[str, Any]) -> Dict[str, Any]:
    return {k: r.get(k) for k in ("rater_id", "display_name", "active", "created_at")}


def rater_for_token(store: RecordStore, token: str) -> Optional[Dict[str, Any]]:
    """The active rater whose token this is, or None. Compares digests in
    constant time; the loop over raters is a handful of small objects."""
    if not token or len(token) > 128:
        return None
    want = _token_digest(token)
    found = None
    for r in list_raters(store):
        if r.get("active") and hmac.compare_digest(str(r.get("token_sha256", "")), want):
            found = r
    return found


# --- the session cookie ---------------------------------------------------------------

def cookie_value(rater_id: str, secret: bytes, now: Optional[float] = None) -> str:
    exp = int((now or _now()) + SESSION_SECONDS)
    body = f"{rater_id}|{exp}"
    sig = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    return f"{body}|{sig}"


def rater_id_from_cookie(value: Optional[str], secret: bytes, now: Optional[float] = None) -> Optional[str]:
    if not value or value.count("|") != 2:
        return None
    rater_id, exp_s, sig = value.split("|")
    if not valid_rater_id(rater_id) or not exp_s.isdigit():
        return None
    want = hmac.new(secret, f"{rater_id}|{exp_s}".encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(sig, want) or int(exp_s) < (now or _now()):
        return None
    return rater_id


# --- assignments ----------------------------------------------------------------------

def make_code(rater_id: str, session_id: str) -> str:
    h = hashlib.sha256(f"rf-rater-code\0{rater_id}\0{session_id}".encode("utf-8")).hexdigest()
    return "RC-" + h[:10].upper()


def assignment_key(rater_id: str, code: str) -> str:
    return f"assignments/{rater_id}/{code}.json"


def list_assignments(store: RecordStore, rater_id: str) -> List[Dict[str, Any]]:
    out = []
    for k in store.list_keys(f"assignments/{rater_id}/"):
        a = store.get(k)
        if a and a.get("kind") == "assignment":
            out.append(a)
    return sorted(out, key=lambda a: (a["data"].get("position") or 0, a.get("created_at") or ""))


def assign(store: RecordStore, source, rater_id: str, session_ids: List[str]) -> Dict[str, Any]:
    """Give a rater these step 1 encounters. Idempotent per (rater, encounter).
    An encounter step 1 does not hold, or holds without a participant key
    (a test or recovered one), is refused by name rather than half-assigned."""
    if not valid_rater_id(rater_id) or store.get(rater_key(rater_id)) is None:
        raise RatingError(f"no rater {rater_id!r}")
    if not isinstance(session_ids, list) or not session_ids:
        raise RatingError("session_ids must be a non-empty list")
    bad = [s for s in session_ids if not valid_session_id(s)]
    if bad:
        raise RatingError(f"not step 1 session ids: {bad}")
    existing = list_assignments(store, rater_id)
    have = {a["step1"]["session_ids"][0]: a for a in existing}
    position = max([a["data"].get("position") or 0 for a in existing], default=0)
    created, skipped, refused = [], [], {}
    for sid in session_ids:
        if sid in have:
            skipped.append(have[sid]["data"]["code"])
            continue
        m = source.manifest(sid)
        if m is None:
            refused[sid] = "not in step 1"
            continue
        pk = m.get("participant_key")
        refs = {"participant_key": pk, "session_ids": [sid], "run_id": m.get("run_id"),
                "run_ids": [m["run_id"]] if m.get("run_id") else []}
        position += 1
        code = make_code(rater_id, sid)
        try:
            rec = new_record("assignment", step1=refs, cohort=m.get("cohort") or "study",
                             data={"rater_id": rater_id, "code": code, "position": position,
                                   "status": "assigned", "done_at": None})
        except (StoreError, ValueError) as exc:
            position -= 1
            refused[sid] = str(exc)
            continue
        store.put(assignment_key(rater_id, code), rec, create_only=True)
        created.append(code)
    return {"rater_id": rater_id, "created": created, "already_assigned": skipped, "refused": refused}


def load_assignment(store: RecordStore, rater_id: str, code: str) -> Optional[Dict[str, Any]]:
    if not valid_rater_id(rater_id) or not CODE_RE.fullmatch(code or ""):
        return None
    a = store.get(assignment_key(rater_id, code))
    return a if a and a.get("kind") == "assignment" else None


def queue_view(assignments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the rater sees of their queue: codes and progress, nothing of step 1."""
    return [{"code": a["data"]["code"], "position": a["data"]["position"],
             "status": a["data"]["status"]} for a in assignments]


# --- the form --------------------------------------------------------------------------

def item_order(rater_id: str, code: str) -> List[Dict[str, Any]]:
    """All items, in an order fixed per rater and code: mixed across constructs
    so the grouping does not show, stable so a reload or a draft lines up."""
    rows = _items.all_items()
    seed = int(hashlib.sha256(f"rf-rater-order\0{rater_id}\0{code}".encode("utf-8")).hexdigest(), 16)
    random.Random(seed).shuffle(rows)
    return [{"key": r["key"], "label": r["label"]} for r in rows]


def validate_submission(payload: Any, *, complete: bool) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(scores, comments). With complete=True every item must be answered."""
    if not isinstance(payload, dict):
        raise RatingError("body must be a JSON object")
    scores_in = payload.get("scores")
    if not isinstance(scores_in, dict):
        raise RatingError("scores must be an object of item_key -> 1..5 or \"na\"")
    keys = _items.item_keys()
    scores: Dict[str, Any] = {}
    for k, v in scores_in.items():
        if k not in keys:
            raise RatingError(f"unknown item {k!r}")
        if v == _items.NA:
            scores[k] = _items.NA
        elif isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
            scores[k] = v
        elif v is None:
            continue
        else:
            raise RatingError(f"item {k!r}: score must be 1..5 or \"na\"")
    if complete:
        missing = [k for k in keys if k not in scores]
        if missing:
            raise RatingError(f"{len(missing)} item(s) unanswered")
    comments_in = payload.get("comments") or {}
    if not isinstance(comments_in, dict):
        raise RatingError("comments must be an object")
    comments = {k: str(comments_in.get(k) or "")[:4000] for k in ("better", "other")}
    return scores, comments


def draft_key(rater_id: str, code: str) -> str:
    return f"drafts/{rater_id}/{code}.json"


def rating_key(rater_id: str, code: str) -> str:
    return f"ratings/{rater_id}/{code}.json"


def rating_record(assignment: Dict[str, Any], scores: Dict[str, Any], comments: Dict[str, str],
                  *, media_kind: Optional[str], transcript_source: Optional[str]) -> Dict[str, Any]:
    rater_id, code = assignment["data"]["rater_id"], assignment["data"]["code"]
    return new_record("rating", step1=assignment["step1"], cohort=assignment["cohort"], data={
        "rater_id": rater_id, "code": code,
        "scores": {k: scores[k] for k in _items.item_keys() if k in scores},
        "comments": comments,
        "item_order": [r["key"] for r in item_order(rater_id, code)],
        "scale": {str(n): label for n, label in _items.SCALE} | {_items.NA: _items.NA_LABEL},
        "media_kind": media_kind, "transcript_source": transcript_source,
    })


def ratings_csv(records: List[Dict[str, Any]]) -> str:
    """Long format, one row per item, for the loader and for a spreadsheet."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["rater_id", "code", "session_id", "participant_key", "run_id", "cohort",
                "construct", "item_key", "score", "rated_at", "record_id"])
    construct_of = _items.item_keys()
    for r in records:
        d, s1 = r.get("data") or {}, r.get("step1") or {}
        for k, v in (d.get("scores") or {}).items():
            w.writerow([d.get("rater_id"), d.get("code"), (s1.get("session_ids") or [None])[0],
                        s1.get("participant_key"), s1.get("run_id"), r.get("cohort"),
                        construct_of.get(k), k, v, r.get("created_at"), r.get("record_id")])
    return buf.getvalue()
