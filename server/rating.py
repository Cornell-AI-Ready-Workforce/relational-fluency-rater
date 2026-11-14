"""Raters, the recordings they are given, and the ratings they submit. Logic only; routes in app.py.

WHO RATES. Trained team raters, not participants (decided 2026-10-22, after the
"Relational Fluency Annotation" prototype). A rater is created by a researcher
(POST /api/raters) and gets a one-time link carrying a token; opening it sets a
signed, HttpOnly cookie and the token leaves the address bar. Everything after
that is keyed by the cookie.

WHAT A RATER GETS. A BATCH of four recordings the app picks itself, the four
stages of the rating screen, opened in order; a completion code once all four
are submitted. The picking rules (allocate_batch):

  - every recording is rated by RATINGS_PER_SESSION (2) raters in all;
  - a rater never gets two recordings of the same participant, in a batch or
    across batches, so a participant's four recordings, rated twice each, go
    to eight raters;
  - only encounters with a participant key, a scenario this app knows, a
    cohort in RATE_COHORTS (default "study"), a recording with every voice in
    it (not the participant's microphone alone) and a participant who spoke;
  - the least-rated recordings first, at random among equals.

A recording's ratings are counted by SLOTS, slots/<session_id>/<n>.json for
n = 1..RATINGS_PER_SESSION, each written create-only, so two raters given work
at the same moment cannot both take the last slot.

BLIND BY CONSTRUCTION. A rater must not learn which participant or scenario
they are rating. So every encounter a rater is given is named by a RATING
CODE, `RC-` and ten hex digits derived from the rater and the step 1 session
id, and nothing the rater-facing routes return carries a session id,
participant key, run id, cohort, scenario, model or character name; the
transcript shows "Participant" and "Character 1..n". The questions are the
scenario's construct's own items (server/items.py), so the competency is not
hidden. tests/test_rating.py greps every rater-facing response for those
shapes.

WHERE IT GOES. This app's bucket, under prefixes server/store.py lists and
infra/terraform mirrors (retention and IAM):

    raters/<rater_id>.json                 who, and the SHA-256 of their token
    assignments/<rater_id>/<code>.json     which encounter the code stands for,
                                           its batch, stage and completion code
    slots/<session_id>/<n>.json            the nth rating a recording is given to
    drafts/<rater_id>/<code>.json          autosaved answers, overwritten freely
    ratings/<rater_id>/<code>.json         the submission, written once

Assignments and ratings are records in the store's sense: they carry the step 1
references (participant_key, session_ids, run_id) so the researcher side can
join them, and the rater side never sees that block.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import items as _items
from .conventions import valid_stored_participant_key
from .store import RecordStore, new_record

RATER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}")
CODE_RE = re.compile(r"RC-[0-9A-F]{10}")
SESSION_SECONDS = 12 * 3600
COOKIE = "rf_rater"
STAGES_PER_BATCH = 4
RATINGS_PER_SESSION = 2
# Letters and digits a person reads back without confusing them (no 0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class RatingError(ValueError):
    pass


class NoWork(RatingError):
    """Nothing left this rater may be given."""


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


def slot_key(session_id: str, n: int) -> str:
    return f"slots/{session_id}/{n}.json"


def rate_cohorts() -> Tuple[str, ...]:
    """The cohorts whose encounters are given out. "study" in production; a
    local test against step 1's own team runs adds "internal"."""
    raw = os.getenv("RATE_COHORTS", "study")
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def list_assignments(store: RecordStore, rater_id: str) -> List[Dict[str, Any]]:
    out = []
    for k in store.list_keys(f"assignments/{rater_id}/"):
        a = store.get(k)
        if a and a.get("kind") == "assignment":
            out.append(a)
    return sorted(out, key=lambda a: (a["data"].get("batch") or 0, a["data"].get("stage") or 0))


def current_batch(assignments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rater's latest batch, its stages in order; [] before the first."""
    if not assignments:
        return []
    n = max(a["data"].get("batch") or 0 for a in assignments)
    return [a for a in assignments if (a["data"].get("batch") or 0) == n]


def batch_done(stages: List[Dict[str, Any]]) -> bool:
    return bool(stages) and all(a["data"]["status"] == "done" for a in stages)


def slot_counts(store: RecordStore) -> Dict[str, int]:
    """session_id -> how many raters it has been given to so far."""
    counts: Dict[str, int] = {}
    for k in store.list_keys("slots/"):
        parts = k.split("/")
        if len(parts) == 3:
            counts[parts[1]] = counts.get(parts[1], 0) + 1
    return counts


def eligible(encounter: Dict[str, Any], cohorts: Tuple[str, ...]) -> bool:
    """Whether a step 1 encounter (a source.index() row) may be given out at all."""
    return (valid_stored_participant_key(encounter.get("participant_key"))
            and bool(encounter.get("run_id"))
            and encounter.get("cohort") in cohorts
            and _items.construct_for_scenario(encounter.get("scenario")) is not None)


def completion_code() -> str:
    pick = lambda n: "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))  # noqa: E731
    return f"RFR-{pick(4)}-{pick(4)}"


def _claim_slot(store: RecordStore, session_id: str, taken: int, rater_id: str, code: str) -> bool:
    for n in range(taken + 1, RATINGS_PER_SESSION + 1):
        if store.put(slot_key(session_id, n), {
                "kind": "slot", "schema": 1, "session_id": session_id, "n": n,
                "rater_id": rater_id, "code": code,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                create_only=True):
            return True
    return False


_ALLOCATE = threading.Lock()


def allocate_batch(store: RecordStore, source, rater_id: str,
                   rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    """The rater's open batch, or a new one when they have none open.

    A new batch is up to STAGES_PER_BATCH recordings, each of a different
    participant and of none this rater has had before. Fewer when fewer are
    left; NoWork when there are none."""
    with _ALLOCATE:
        mine = list_assignments(store, rater_id)
        open_batch = current_batch(mine)
        if open_batch and not batch_done(open_batch):
            return open_batch
        seen = {a["step1"]["participant_key"] for a in mine}
        counts = slot_counts(store)
        cohorts = rate_cohorts()
        pool = [e for e in source.index().values()
                if eligible(e, cohorts) and e["participant_key"] not in seen
                and counts.get(e["session_id"], 0) < RATINGS_PER_SESSION]
        (rng or random.SystemRandom()).shuffle(pool)
        pool.sort(key=lambda e: counts.get(e["session_id"], 0))  # stable: random among equals
        batch = max([a["data"].get("batch") or 0 for a in mine], default=0) + 1
        code_for_batch = completion_code()
        chosen: List[Dict[str, Any]] = []
        for e in pool:
            if len(chosen) == STAGES_PER_BATCH:
                break
            sid, pk = e["session_id"], e["participant_key"]
            if pk in seen or not source.rateable(sid):
                continue
            code = make_code(rater_id, sid)
            if not _claim_slot(store, sid, counts.get(sid, 0), rater_id, code):
                continue
            seen.add(pk)
            rec = new_record(
                "assignment", cohort=e["cohort"],
                step1={"participant_key": pk, "session_ids": [sid], "run_id": e["run_id"],
                       "run_ids": [e["run_id"]]},
                data={"rater_id": rater_id, "code": code, "batch": batch,
                      "stage": len(chosen) + 1, "scenario": e.get("scenario"),
                      "construct": _items.construct_for_scenario(e.get("scenario")),
                      "completion_code": code_for_batch, "status": "assigned", "done_at": None})
            store.put(assignment_key(rater_id, code), rec, create_only=True)
            chosen.append(rec)
        if not chosen:
            raise NoWork("There are no recordings for you to rate right now.")
        return chosen


def load_assignment(store: RecordStore, rater_id: str, code: str) -> Optional[Dict[str, Any]]:
    if not valid_rater_id(rater_id) or not CODE_RE.fullmatch(code or ""):
        return None
    a = store.get(assignment_key(rater_id, code))
    return a if a and a.get("kind") == "assignment" else None


def batch_view(stages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the rater sees of a batch: codes and progress, nothing of step 1,
    and the completion code only once every stage is submitted."""
    if not stages:
        return {"batch": None, "stages": [], "done": False, "completion_code": None}
    done = batch_done(stages)
    return {"batch": stages[0]["data"].get("batch"),
            "stages": [{"code": a["data"]["code"], "stage": a["data"].get("stage"),
                        "status": a["data"]["status"]} for a in stages],
            "done": done,
            "completion_code": stages[0]["data"].get("completion_code") if done else None}


# --- the form --------------------------------------------------------------------------

def item_order(rater_id: str, code: str, construct: str) -> List[Dict[str, str]]:
    """The construct's items, in an order fixed per rater and code: mixed, and
    stable so a reload or a draft lines up."""
    rows = _items.construct_items(construct)
    seed = int(hashlib.sha256(f"rf-rater-order\0{rater_id}\0{code}".encode("utf-8")).hexdigest(), 16)
    random.Random(seed).shuffle(rows)
    return rows


def needs_example(score: Any) -> bool:
    """A score of 2 to 5 says the rater saw it, so it needs one example."""
    return isinstance(score, int) and score >= 2


MAX_EXAMPLES = 20
MAX_CLIP_END_S = 4 * 3600.0


def _example(x: Any, lines: Dict[int, Dict[str, Any]], video: bool = False) -> Optional[Dict[str, Any]]:
    """{"line": i} (a participant line of the transcript), {"clip": [a, b]} in
    seconds, or {"shot": t}, a screenshot: one moment of the video, in seconds,
    taken only when the recording has a picture. A line keeps its time and text
    beside it. A screenshot is stored as its moment, not as an image: the frame
    is in step 1's recording at that second, so no picture of a participant is
    copied into this app's bucket."""
    if isinstance(x, dict) and set(x) == {"line"}:
        i = x["line"]
        if isinstance(i, int) and not isinstance(i, bool) and i in lines:
            return {"line": i, "t": lines[i]["t"], "text": lines[i]["text"]}
    if isinstance(x, dict) and set(x) == {"clip"} and isinstance(x["clip"], list) and len(x["clip"]) == 2:
        a, b = x["clip"]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)) \
                and 0 <= a and b - a >= 1 and b <= MAX_CLIP_END_S:
            return {"clip": [round(float(a), 1), round(float(b), 1)]}
    if video and isinstance(x, dict) and set(x) == {"shot"}:
        t = x["shot"]
        if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t <= MAX_CLIP_END_S:
            return {"shot": round(float(t), 1)}
    return None


def validate_submission(payload: Any, *, keys: List[str], lines: Dict[int, Dict[str, Any]],
                        complete: bool, video: bool = False) -> Tuple[Dict[str, int], Dict[str, List], Dict[str, str]]:
    """(scores, examples, comments) for one encounter's items.

    `keys` are the encounter's items, `lines` the participant lines that may be
    given as examples (transcript index -> row), `video` whether the recording
    has a picture, so a screenshot may be one. With complete=True every item
    must be scored and every score of 2 to 5 have an example."""
    if not isinstance(payload, dict):
        raise RatingError("body must be a JSON object")
    scores_in = payload.get("scores")
    if not isinstance(scores_in, dict):
        raise RatingError("scores must be an object of item_key -> 1..5")
    scores: Dict[str, int] = {}
    for k, v in scores_in.items():
        if k not in keys:
            raise RatingError(f"unknown item {k!r}")
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
            scores[k] = v
        elif v is not None:
            raise RatingError(f"item {k!r}: score must be 1..5")
    examples_in = payload.get("examples") or {}
    if not isinstance(examples_in, dict):
        raise RatingError("examples must be an object of item_key -> list")
    examples: Dict[str, List] = {}
    for k, xs in examples_in.items():
        if k not in keys:
            raise RatingError(f"unknown item {k!r}")
        if not isinstance(xs, list) or len(xs) > MAX_EXAMPLES:
            raise RatingError(f"item {k!r}: examples must be a list of at most {MAX_EXAMPLES}")
        out = []
        for x in xs:
            ex = _example(x, lines, video)
            if ex is None:
                raise RatingError(f"item {k!r}: an example is {{\"line\": n}} for a participant "
                                  f"line, {{\"clip\": [start, end]}} in seconds, or "
                                  f"{{\"shot\": seconds}} when the recording is a video")
            out.append(ex)
        if out:
            examples[k] = out
    if complete:
        missing = [k for k in keys if k not in scores]
        if missing:
            raise RatingError(f"{len(missing)} item(s) unanswered")
        bare = [k for k in keys if needs_example(scores[k]) and not examples.get(k)]
        if bare:
            raise RatingError(f"{len(bare)} answer(s) of 2 to 5 without an example")
        examples = {k: v for k, v in examples.items() if needs_example(scores[k])}
    comments_in = payload.get("comments") or {}
    if not isinstance(comments_in, dict):
        raise RatingError("comments must be an object")
    comments = {k: str(comments_in.get(k) or "")[:4000] for k in ("better", "other")}
    return scores, examples, comments


def draft_key(rater_id: str, code: str) -> str:
    return f"drafts/{rater_id}/{code}.json"


def rating_key(rater_id: str, code: str) -> str:
    return f"ratings/{rater_id}/{code}.json"


def rating_record(assignment: Dict[str, Any], scores: Dict[str, int], examples: Dict[str, List],
                  comments: Dict[str, str], *, media_kind: Optional[str],
                  transcript_source: Optional[str]) -> Dict[str, Any]:
    d = assignment["data"]
    rater_id, code, construct = d["rater_id"], d["code"], d["construct"]
    order = [r["key"] for r in item_order(rater_id, code, construct)]
    return new_record("rating", step1=assignment["step1"], cohort=assignment["cohort"], data={
        "rater_id": rater_id, "code": code, "batch": d.get("batch"), "stage": d.get("stage"),
        "construct": construct,
        "scores": {k: scores[k] for k in order if k in scores},
        "examples": {k: examples[k] for k in order if k in examples},
        "comments": comments,
        "item_order": order,
        "scale": {str(n): label for n, label in _items.SCALE},
        "media_kind": media_kind, "transcript_source": transcript_source,
    })


def ratings_csv(records: List[Dict[str, Any]]) -> str:
    """Long format, one row per item, for the loader and for a spreadsheet."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["rater_id", "code", "session_id", "participant_key", "run_id", "cohort",
                "construct", "item_key", "score", "rated_at", "record_id", "examples"])
    construct_of = _items.item_keys()
    for r in records:
        d, s1 = r.get("data") or {}, r.get("step1") or {}
        for k, v in (d.get("scores") or {}).items():
            ex = (d.get("examples") or {}).get(k) or []
            w.writerow([d.get("rater_id"), d.get("code"), (s1.get("session_ids") or [None])[0],
                        s1.get("participant_key"), s1.get("run_id"), r.get("cohort"),
                        construct_of.get(k), k, v, r.get("created_at"), r.get("record_id"),
                        json.dumps(ex, ensure_ascii=False) if ex else ""])
    return buf.getvalue()
