"""FastAPI server for relational-fluency-rater (step 2 of IRB0151104).

Same shape as relational-fluency (step 1): one FastAPI process, plain HTML pages
from static/, run with `python -m server.app`, a public /health for the load
balancer and the deploy guard, and a researcher key for everything that reaches
study data. Patterns below are adapted from relational-fluency@f9947d2
server/app.py; the places they differ say why.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .build_info import build_sha

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


def main() -> int:
    import uvicorn

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    why = _refuse_unprotected_public_start()
    if why:
        print(f"REFUSING TO START: {why}", file=sys.stderr)
        return 2
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8766"))  # step 1 is 8765; both can run at once
    uvicorn.run("server.app:app", host=host, port=port, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
