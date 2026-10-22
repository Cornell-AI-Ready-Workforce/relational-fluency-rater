"""FastAPI server for relational-fluency-rater (step 2 of IRB0151104).

Same shape as relational-fluency (step 1): one FastAPI process, plain HTML pages
from static/, run with `python -m server.app`, a public /health for the load
balancer and the deploy guard, and a researcher key for everything that reaches
study data. Patterns below are adapted from relational-fluency@f9947d2
server/app.py; the places they differ say why.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import items as _items
from . import rating as _rating
from . import source as _source
from . import store as _store
from .build_info import build_sha
from .conventions import normalize_cohort, normalize_participant_key, valid_stored_participant_key

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"

# The researcher key: Secrets Manager relational-fluency-rater/researcher-key
# in production. Read at import into a module global, as step 1 does with
# SESSION_KEY, so tests can monkeypatch it.
RESEARCHER_KEY = os.getenv("RESEARCHER_KEY", "").strip()

APP_HOST = os.getenv("APP_HOST", "rater.ai-ready-workforce.ai.cornell.edu").strip()
_DEFAULT_ALLOWED = f"{APP_HOST},localhost,127.0.0.1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", _DEFAULT_ALLOWED).split(",")
                 if h.strip()]

app = FastAPI(title="Relational Fluency Rater")

# Behind the ALB, honour X-Forwarded-Proto so generated URLs are https.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Host-header allowlist, exempting /health: the ALB health check addresses the
# task by its private IP, which no allowlist can contain (same as step 1).
if ALLOWED_HOSTS:

    @app.middleware("http")
    async def _guard_host(request, call_next):
        if request.url.path != "/health":
            host = (request.headers.get("host") or "").split(":")[0]
            if host and host not in ALLOWED_HOSTS:
                return JSONResponse({"detail": "Invalid host header"}, status_code=400)
        return await call_next(request)


@app.middleware("http")
async def _security_headers(request, call_next):
    # Enforces what step 1 only promises: a page of this app contacts nobody
    # else. 'self' only, so a font CDN or an external script added later fails
    # in the browser instead of quietly working. 'unsafe-inline' for the inline
    # <script>/<style> blocks the pages use, as step 1's pages do.
    resp = await call_next(request)
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- researcher key ---------------------------------------------------------------
#
# Differs from step 1 deliberately: step 1's check_key and /ws/researcher
# compare with `!=`; here every comparison is constant-time, and an EMPTY key is
# open only to a process that cannot be reached from elsewhere (see
# _refuse_unprotected_public_start, which stops any other process from serving).

def is_researcher(key: Optional[str]) -> bool:
    if not RESEARCHER_KEY:
        return True  # local development only; a public start is refused
    return bool(key) and secrets.compare_digest(key, RESEARCHER_KEY)


def check_key(key: Optional[str]) -> None:
    if not is_researcher(key):
        raise HTTPException(status_code=401, detail="Bad or missing key")


# --- active sessions ----------------------------------------------------------------
#
# /health's active_sessions is what tools/deploy.sh refuses to plan over, as in
# step 1. Step 1 counts open voice sessions; this app has no socket to count,
# so a session is a participant who has touched the app within the last
# ACTIVE_WINDOW_SECONDS. A deploy replaces the task about two minutes after the
# new one is healthy; whoever is mid-visit then loses their page state.

ACTIVE_WINDOW_SECONDS = int(os.getenv("ACTIVE_WINDOW_SECONDS", "1800"))


class ActivityRegistry:
    def __init__(self) -> None:
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def touch(self, visit_id: str) -> None:
        with self._lock:
            self._seen[visit_id] = time.time()

    def end(self, visit_id: str) -> None:
        with self._lock:
            self._seen.pop(visit_id, None)

    def active(self, now: Optional[float] = None) -> int:
        cutoff = (now or time.time()) - ACTIVE_WINDOW_SECONDS
        with self._lock:
            for k in [k for k, t in self._seen.items() if t < cutoff]:
                del self._seen[k]
            return len(self._seen)


activity = ActivityRegistry()


# --- health -------------------------------------------------------------------------

@app.get("/health")
async def health(key: Optional[str] = Query(None)) -> dict:
    """Liveness probe for the ALB and the deploy guard. Public, always 200, and
    dependency-free: it never calls S3, so a slow bucket cannot make a healthy
    task look dead. Nothing here may say more than a stranger may know."""
    return {
        "status": "ok",
        "ready": True,
        "researcher_key_configured": bool(RESEARCHER_KEY),
        "active_sessions": activity.active(),
        # The commit this image was built from (server/build_info.py); null for a
        # local checkout. tools/check_prod_build.py compares it with the pin.
        "build": build_sha(),
    }


# --- pages ------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "landing.html").read_text(encoding="utf-8"))


# --- entry from Qualtrics ------------------------------------------------------------
#
# Participants arrive the way they arrive at step 1: a link in the Qualtrics
# survey with their participant key piped in,
#
#   https://rater.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/participantId}&qid=${e://Field/ResponseID}
#
# The key is judged by step 1's own normalize_participant_key, and the cohort
# follows step 1's rule: "study" for a usable key, "unattributed" for one the
# survey failed to pipe (the participant still proceeds, under a unique
# synthetic key, so nobody is turned away and nobody collides), and ?cohort=
# is honoured only with the researcher key. A usable key finds the
# participant's step 1 encounters, and the entry record written here names
# them (server/store.py).
#
# A GET that is a link preview, a prefetch or a scanner, or that carries no
# usable key, gets the entry check page and writes nothing; a person presses
# Continue, which POSTs the same parameters. Same rule as step 1's /start, for
# the same reason: the link lives in places that fetch URLs nobody clicked.

def entry_params(
    request: Request,
    key: Optional[str] = None,
    pid: Optional[str] = None,
    participant_id: Optional[str] = None,
    participantId: Optional[str] = None,
    PROLIFIC_PID: Optional[str] = None,
    qid: Optional[str] = None,
    cohort: Optional[str] = None,
) -> Dict[str, Any]:
    """The query parameters step 1's entry links accept, same spellings."""
    return {"key": key, "pid": pid, "participant_id": participant_id,
            "participantId": participantId, "PROLIFIC_PID": PROLIFIC_PID,
            "qid": qid, "cohort": cohort, "request": request}


_PROBE_AGENTS = ("bot", "crawler", "spider", "preview", "slack", "facebookexternalhit",
                 "discord", "whatsapp", "telegram", "skype", "teams", "outlook",
                 "microsoft office", "proofpoint", "mimecast", "barracuda", "zoom",
                 "curl/", "python-requests", "wget")
_PROBE_HEADERS = (("purpose", "prefetch"), ("sec-purpose", "prefetch"),
                  ("x-purpose", "preview"), ("x-moz", "prefetch"))

# A Qualtrics ResponseID (R_ plus letters and digits). Only a real one is used
# to resume an unattributed arrival; the unreplaced ${e://Field/ResponseID} is
# one string shared by everybody.
_QID_RE = re.compile(r"R_[A-Za-z0-9]{8,32}")


def _link_probe_reason(request: Request) -> Optional[str]:
    ua = (request.headers.get("user-agent") or "").strip().lower()
    if not ua:
        return "no user agent"
    for a in _PROBE_AGENTS:
        if a in ua:
            return f"user agent looks like a link fetcher ({a})"
    for h, v in _PROBE_HEADERS:
        if v in (request.headers.get(h) or "").lower():
            return f"{h}: {v}"
    return None


def _entry_id(participant_key: str) -> str:
    return "e_" + hashlib.sha256(f"rf-rater-entry\0{participant_key}".encode()).hexdigest()[:24]


_VISIT_RE = re.compile(r"e_[0-9a-f]{24}")


def _page(name: str, **fill: str) -> str:
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    for k, v in fill.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _check_page(p: Dict[str, Any], message: str) -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(str(v), quote=True)}" />'
        for k, v in p.items() if k != "request" and v is not None)
    return HTMLResponse(_page("start.html", message=html.escape(message), hidden=hidden),
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


async def _form_or_query(request: Request) -> Dict[str, Any]:
    p = entry_params(request, **{k: v for k, v in request.query_params.items()
                                 if k in ("key", "pid", "participant_id", "participantId",
                                          "PROLIFIC_PID", "qid", "cohort")})
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        from urllib.parse import parse_qsl
        for k, v in parse_qsl(body):
            if k in p and k != "request":
                p[k] = v
    return p


def _enter(p: Dict[str, Any], *, confirmed: bool):
    request: Request = p["request"]
    raw_key = p.get("pid") or p.get("participant_id") or p.get("participantId") or p.get("PROLIFIC_PID")
    pkey, status = normalize_participant_key(raw_key)

    if not confirmed:
        probe = _link_probe_reason(request)
        if probe or pkey is None:
            return _check_page(p, "Press Continue to start this part of the study.")

    cohort_param = p.get("cohort")
    if cohort_param and is_researcher(p.get("key")):
        try:
            cohort = normalize_cohort(cohort_param)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    else:
        cohort = "study" if status == "ok" else "unattributed"

    qid = (p.get("qid") or "").strip()
    qid_ok = bool(_QID_RE.fullmatch(qid))
    if pkey is None:
        print(f"  WARNING: /start got an unusable participant key ({status}): {raw_key!r}. "
              f"Continuing in cohort '{cohort}'. Check the Qualtrics piping.")
        # Resume on the survey response when there is a real one, so a second
        # press of Continue is the same person, not a second record.
        seed = f"qid:{qid}" if qid_ok else secrets.token_hex(6)
        pkey = "unattributed_" + hashlib.sha256(seed.encode()).hexdigest()[:12]

    entry_id = _entry_id(pkey)
    store = _store.get_store()
    key = f"entries/{entry_id}.json"
    if store.get(key) is None:
        try:
            encounters = _source.get_source().encounters_for(pkey) if status == "ok" else []
        except Exception as exc:  # S3 unreachable: say so, write nothing
            print(f"  ERROR: step 1 source unavailable at /start: {type(exc).__name__}: {exc}")
            return HTMLResponse(_page("landing.html"), status_code=503,
                                headers={"Retry-After": "30", "Cache-Control": "no-store"})
        rec = _store.new_record(
            "entry", step1=_store.step1_refs(pkey, encounters), cohort=cohort,
            data={"entry_id": entry_id, "qualtrics_id": qid or None,
                  "participant_key_status": status,
                  # Kept only when unusable, for a later hand-join (as step 1 does).
                  "raw_participant_key": None if status == "ok" else (raw_key or None),
                  "step1_source": _source.get_source().kind})
        store.put(key, rec, create_only=True)
    activity.touch(entry_id)
    return RedirectResponse(f"/visit?v={entry_id}", status_code=303)


# Sync handlers: FastAPI runs them on its threadpool, which is where the S3
# manifest scan and the record write belong, not on the event loop.
@app.get("/start")
def start(p: Dict[str, Any] = Depends(entry_params)):
    return _enter(p, confirmed=False)


@app.post("/start")
async def start_confirmed(request: Request):
    p = await _form_or_query(request)
    return await run_in_threadpool(_enter, p, confirmed=True)


@app.get("/visit", response_class=HTMLResponse)
def visit(v: str = Query("")) -> HTMLResponse:
    if not _VISIT_RE.fullmatch(v or "") or _store.get_store().get(f"entries/{v}.json") is None:
        raise HTTPException(status_code=404, detail="Not found")
    activity.touch(v)
    return HTMLResponse(_page("visit.html", build=html.escape(build_sha() or "")),
                        headers={"Cache-Control": "no-store"})


# --- researcher API -------------------------------------------------------------------

@app.get("/api/entries")
def api_entries(key: Optional[str] = None, cohort: Optional[str] = None) -> dict:
    check_key(key)
    store = _store.get_store()
    out = []
    for k in store.list_keys("entries/"):
        rec = store.get(k) or {}
        if cohort and rec.get("cohort") != cohort:
            continue
        out.append({"record_id": rec.get("record_id"), "created_at": rec.get("created_at"),
                    "cohort": rec.get("cohort"), "step1": rec.get("step1"),
                    "qualtrics_id": (rec.get("data") or {}).get("qualtrics_id")})
    return {"entries": out, "count": len(out), "store": store.kind}


@app.get("/api/step1/participants/{participant_key}/encounters")
def api_step1_encounters(participant_key: str, key: Optional[str] = None) -> dict:
    """What this app sees of a participant in step 1: for checking the mapping."""
    check_key(key)
    if not valid_stored_participant_key(participant_key):
        raise HTTPException(status_code=400, detail="Not a participant key")
    src = _source.get_source()
    return {"participant_key": participant_key, "source": src.kind,
            "encounters": src.encounters_for(participant_key)}



# --- raters: sign-in, queue, rating screen ---------------------------------------------
#
# Step 2's task (decided 2026-10-22): trained team raters score step 1's
# encounters. A researcher creates a rater (POST /api/raters) and hands them the
# one-time link it returns; opening it sets the session cookie and redirects to
# /rate, so the token leaves the address bar. Everything rater-facing is then
# keyed by that cookie and by RATING CODES, never by step 1's ids
# (server/rating.py says what a rater may and may not see).

_SESSION_SECRET_FALLBACK = secrets.token_bytes(32)  # local only: sessions end with the process


def _session_secret() -> bytes:
    # Derived from the researcher key so the deployment needs no second secret;
    # a different label, so the cookie signature is not the key itself.
    if RESEARCHER_KEY:
        return hmac.new(RESEARCHER_KEY.encode("utf-8"), b"rater-session", hashlib.sha256).digest()
    return _SESSION_SECRET_FALLBACK


def _rater_from(request: Request) -> Optional[str]:
    return _rating.rater_id_from_cookie(request.cookies.get(_rating.COOKIE), _session_secret())


def _require_rater(request: Request) -> str:
    rid = _rater_from(request)
    if not rid:
        raise HTTPException(status_code=401, detail="Not signed in as a rater")
    activity.touch(f"rater:{rid}")
    return rid


def _assignment_or_404(rater_id: str, code: str) -> Dict[str, Any]:
    a = _rating.load_assignment(_store.get_store(), rater_id, code)
    if a is None:
        raise HTTPException(status_code=404, detail="Not found")
    return a


_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


@app.get("/rate", response_class=HTMLResponse)
def rate_home(request: Request, token: Optional[str] = Query(None)):
    if token:
        rater = _rating.rater_for_token(_store.get_store(), token)
        if rater is None:
            return HTMLResponse(_page("rater-signin.html", message=html.escape(
                "That link is not valid. Ask the study team for a new one.")),
                status_code=401, headers=_NO_STORE)
        resp = RedirectResponse("/rate", status_code=303)
        resp.set_cookie(_rating.COOKIE, _rating.cookie_value(rater["rater_id"], _session_secret()),
                        max_age=_rating.SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
        return resp
    rid = _rater_from(request)
    if not rid:
        return HTMLResponse(_page("rater-signin.html", message=html.escape(
            "Open the link the study team gave you to see your queue.")), headers=_NO_STORE)
    activity.touch(f"rater:{rid}")
    return HTMLResponse(_page("queue.html", rater_id=html.escape(rid),
                              build=html.escape(build_sha() or "")), headers=_NO_STORE)


@app.get("/api/queue")
def api_queue(request: Request) -> dict:
    rid = _require_rater(request)
    rows = _rating.queue_view(_rating.list_assignments(_store.get_store(), rid))
    return {"rater_id": rid, "queue": rows, "done": sum(r["status"] == "done" for r in rows),
            "total": len(rows)}


@app.get("/rate/{code}", response_class=HTMLResponse)
def rate_page(code: str, request: Request):
    rid = _rater_from(request)
    if not rid:
        return RedirectResponse("/rate", status_code=303)
    _assignment_or_404(rid, code)
    activity.touch(f"rater:{rid}")
    return HTMLResponse(_page("rate.html", code=html.escape(code), rater_id=html.escape(rid),
                              build=html.escape(build_sha() or "")), headers=_NO_STORE)


def _transcript_rows(session_id: str) -> Dict[str, Any]:
    """Turns as the rater sees them: time, Participant or Character n, text."""
    src = _source.get_source()
    rec = src.record(session_id) or {}
    cast = [c.get("id") for c in (rec.get("cast") or []) if isinstance(c, dict)]
    label: Dict[str, str] = {}
    for turn in rec.get("transcript") or []:
        aid = turn.get("agent_id")
        if turn.get("role") not in ("participant", "user") and aid and aid not in label:
            if aid not in cast:
                cast.append(aid)
    for i, aid in enumerate(cast, 1):
        label[aid] = f"Character {i}" if len(cast) > 1 else "Character"
    rows = []
    for turn in rec.get("transcript") or []:
        text = (turn.get("text") or "").strip()
        is_participant = turn.get("role") in ("participant", "user")
        rows.append({"t": float(turn.get("t") or 0.0),
                     "who": "Participant" if is_participant else label.get(turn.get("agent_id"), "Character"),
                     "text": text})
    tr = src.transcript(session_id)
    return {"rows": rows, "participant_source": tr.get("participant_source"),
            "characters": len(cast)}


@app.get("/api/rate/{code}")
def api_rate(code: str, request: Request) -> dict:
    rid = _require_rater(request)
    store = _store.get_store()
    a = _assignment_or_404(rid, code)
    sid = a["step1"]["session_ids"][0]
    src = _source.get_source()
    try:
        transcript = _transcript_rows(sid)
        media = src.media_choice(sid)
    except Exception as exc:  # step 1 unreachable: say so, nothing else
        print(f"  ERROR: step 1 source unavailable for {code}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=503, detail="Recordings are unavailable right now; try again shortly.")
    queue = _rating.queue_view(_rating.list_assignments(store, rid))
    draft = store.get(_rating.draft_key(rid, code)) or {}
    return {
        "code": code, "rater_id": rid,
        "position": a["data"]["position"], "total": len(queue), "status": a["data"]["status"],
        "media": ({"kind": media["kind"], "url": f"/rate/{code}/media"} if media else None),
        "transcript": transcript["rows"], "characters": transcript["characters"],
        "items": _rating.item_order(rid, code),
        "scale": [{"value": n, "label": l} for n, l in _items.SCALE] +
                 [{"value": _items.NA, "label": _items.NA_LABEL}],
        "draft": {"scores": draft.get("scores") or {}, "comments": draft.get("comments") or {}},
    }


@app.put("/api/rate/{code}/draft")
def api_rate_draft(code: str, request: Request, payload: Any = Body(...)) -> dict:
    rid = _require_rater(request)
    a = _assignment_or_404(rid, code)
    if a["data"]["status"] == "done":
        raise HTTPException(status_code=409, detail="Already submitted")
    try:
        scores, comments = _rating.validate_submission(payload, complete=False)
    except _rating.RatingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _store.get_store().put(_rating.draft_key(rid, code), {
        "kind": "draft", "schema": 1, "rater_id": rid, "code": code, "scores": scores,
        "comments": comments, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"saved": True, "answered": len(scores), "of": len(_items.item_keys())}


@app.post("/api/rate/{code}/submit", status_code=201)
def api_rate_submit(code: str, request: Request, payload: Any = Body(...)) -> dict:
    rid = _require_rater(request)
    store = _store.get_store()
    a = _assignment_or_404(rid, code)
    if a["data"]["status"] == "done" or store.get(_rating.rating_key(rid, code)) is not None:
        raise HTTPException(status_code=409, detail="Already submitted")
    try:
        scores, comments = _rating.validate_submission(payload, complete=True)
    except _rating.RatingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sid = a["step1"]["session_ids"][0]
    src = _source.get_source()
    try:
        media = src.media_choice(sid)
        tsrc = src.transcript(sid).get("participant_source")
    except Exception:
        media, tsrc = None, None
    rec = _rating.rating_record(a, scores, comments, media_kind=media["kind"] if media else None,
                                transcript_source=tsrc)
    if not store.put(_rating.rating_key(rid, code), rec, create_only=True):
        raise HTTPException(status_code=409, detail="Already submitted")
    a["data"]["status"] = "done"
    a["data"]["done_at"] = rec["created_at"]
    store.put(_rating.assignment_key(rid, code), a)
    activity.end(f"rater:{rid}")
    return {"submitted": True, "code": code, "record_id": rec["record_id"]}


@app.get("/rate/{code}/media")
def rate_media(code: str, request: Request):
    """The recording for a code, streamed by this app so the step 1 object key
    (which carries the session id) never reaches the browser."""
    rid = _require_rater(request)
    a = _assignment_or_404(rid, code)
    sid = a["step1"]["session_ids"][0]
    src = _source.get_source()
    choice = src.media_choice(sid)
    if choice is None:
        raise HTTPException(status_code=404, detail="No recording")
    local = src.media_local_path(sid, choice["name"])
    if local is not None:
        return FileResponse(local, media_type=choice["content_type"], headers=_NO_STORE)
    resp = src.media_stream(sid, choice["name"], request.headers.get("range"))
    if resp is None:
        raise HTTPException(status_code=404, detail="No recording")
    if resp.get("status") == 416:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{choice['size']}"})
    headers = {**_NO_STORE, "Accept-Ranges": "bytes",
               "Content-Length": str(resp.get("ContentLength") or "")}
    if resp.get("ContentRange"):
        headers["Content-Range"] = resp["ContentRange"]
    return StreamingResponse(resp["Body"].iter_chunks(1 << 16),
                             status_code=206 if resp.get("ContentRange") else 200,
                             media_type=choice["content_type"], headers=headers)


# --- researcher API: raters, assignments, ratings -----------------------------------------

@app.post("/api/raters", status_code=201)
def api_raters_create(key: Optional[str] = None, payload: Dict[str, Any] = Body(...)) -> dict:
    check_key(key)
    try:
        rec, token = _rating.new_rater(_store.get_store(), str(payload.get("rater_id") or ""),
                                       str(payload.get("display_name") or ""))
    except _rating.RatingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"rater": _rating.public_rater(rec), "token": token,
            "link": f"https://{APP_HOST}/rate?token={token}",
            "note": "The token is shown once and stored only as a digest."}


@app.get("/api/raters")
def api_raters_list(key: Optional[str] = None) -> dict:
    check_key(key)
    store = _store.get_store()
    out = []
    for r in _rating.list_raters(store):
        q = _rating.queue_view(_rating.list_assignments(store, r["rater_id"]))
        out.append({**_rating.public_rater(r), "assigned": len(q),
                    "done": sum(x["status"] == "done" for x in q)})
    return {"raters": out, "count": len(out)}


@app.post("/api/assignments", status_code=201)
def api_assignments_create(key: Optional[str] = None, payload: Dict[str, Any] = Body(...)) -> dict:
    """{"rater_id": ..., "session_ids": [...]}: give a rater these step 1 encounters."""
    check_key(key)
    try:
        return _rating.assign(_store.get_store(), _source.get_source(),
                              str(payload.get("rater_id") or ""), payload.get("session_ids"))
    except _rating.RatingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/assignments")
def api_assignments_list(key: Optional[str] = None, rater_id: Optional[str] = None) -> dict:
    check_key(key)
    store = _store.get_store()
    raters = [rater_id] if rater_id else [r["rater_id"] for r in _rating.list_raters(store)]
    out = []
    for rid in raters:
        for a in _rating.list_assignments(store, rid):
            out.append({**a["data"], "step1": a["step1"], "cohort": a["cohort"],
                        "assigned_at": a["created_at"]})
    return {"assignments": out, "count": len(out)}


@app.get("/api/ratings")
def api_ratings(key: Optional[str] = None, format: str = "json"):
    check_key(key)
    store = _store.get_store()
    recs = [r for r in (store.get(k) for k in store.list_keys("ratings/")) if r and r.get("kind") == "rating"]
    if format == "csv":
        return PlainTextResponse(_rating.ratings_csv(recs), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="ratings.csv"'})
    return {"ratings": recs, "count": len(recs), "store": store.kind}

# --- startup ------------------------------------------------------------------------

def _refuse_unprotected_public_start() -> Optional[str]:
    """Why this process must not serve, or None if it may. Same rule as step 1:
    an empty researcher key is fine on a loopback bind and nowhere else."""
    if RESEARCHER_KEY:
        return None
    host = os.getenv("HOST", "127.0.0.1").strip()
    if host in ("127.0.0.1", "localhost", "::1", ""):
        return None
    local = ("localhost", "127.0.0.1", "::1", "testserver")
    public = [h for h in ALLOWED_HOSTS if h not in local]
    if not public and ALLOWED_HOSTS:
        return None
    return ("RESEARCHER_KEY is empty but this process binds " + host + " and answers to "
            + (", ".join(public) if public else "any Host header (ALLOWED_HOSTS is empty)")
            + ". Every researcher route would be open to anyone who finds the hostname. "
              "Set RESEARCHER_KEY, or bind HOST=127.0.0.1.")


def _refuse_public_start_without_buckets() -> Optional[str]:
    """A process reachable from elsewhere must read step 1 from its bucket and
    write to this app's. Without RATER_BUCKET records would go to the
    container's disk, which the next deploy destroys (and which is read-only
    in production, so every entry would fail)."""
    host = os.getenv("HOST", "127.0.0.1").strip()
    local = ("localhost", "127.0.0.1", "::1", "testserver")
    if host in ("127.0.0.1", "localhost", "::1", "") or (
            ALLOWED_HOSTS and all(h in local for h in ALLOWED_HOSTS)):
        return None
    missing = [n for n in ("SOURCE_BUCKET", "RATER_BUCKET") if not os.getenv(n, "").strip()]
    if missing:
        return (f"{' and '.join(missing)} unset on a public bind ({host}); this deployment "
                f"would not read step 1 from S3 or would write records to local disk.")
    return None


def main() -> int:
    import uvicorn

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    why = _refuse_unprotected_public_start() or _refuse_public_start_without_buckets()
    if why:
        print(f"REFUSING TO START: {why}", file=sys.stderr)
        return 2
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8766"))  # step 1 is 8765; both can run at once
    uvicorn.run("server.app:app", host=host, port=port, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
