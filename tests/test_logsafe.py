"""No secret that travels in a URL reaches the access log (server/logsafe.py).

Found 2026-11-08, with the app live and no rater created yet: the researcher
key arrived only as ?key=, a rater's sign-in token as /rate?token= (good for
as long as the rater is active), and uvicorn's access log, which is CloudWatch
in production, writes every path with its query string. These tests hold the
three parts of the fix: query values are dropped from the access record, both
ways of installing that are in place, and the key can be sent as a header.
"""
from __future__ import annotations

import io
import logging
import logging.config

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import logsafe

SECRET = "s3cr3t-Value_9"
RK = "rk"


# --- the redaction ---------------------------------------------------------------

@pytest.mark.parametrize("given,logged", [
    ("/health", "/health"),
    ("/rate", "/rate"),
    (f"/rate?token={SECRET}", "/rate?token=[removed]"),
    (f"/api/entries?key={SECRET}&cohort=study", "/api/entries?key=[removed]&cohort=[removed]"),
    # Every value goes, whatever the name: a name list would miss the next one.
    (f"/start?pid=abc123&qid=R_x&secret={SECRET}", "/start?pid=[removed]&qid=[removed]&secret=[removed]"),
    (f"/x?k={SECRET}&k={SECRET}", "/x?k=[removed]&k=[removed]"),
    (f"/api/ratings?format=csv&key={SECRET}", "/api/ratings?format=[removed]&key=[removed]"),
    ("/x?", "/x"),
    ("/x?&&&", "/x"),
    # Not a name=value pair, or not an ordinary name: dropped whole, so a
    # secret sent where a name goes does not survive as the "name".
    (f"/rate?{SECRET}", "/rate?[removed]"),
    ("/x?flag", "/x?[removed]"),
    ("/rate?" + "A" * 43 + "=1", "/rate?[removed]"),
    (f"/x?%7A{SECRET}=1", "/x?[removed]"),
    (f"/x?key={SECRET}&{SECRET}", "/x?key=[removed]&[removed]"),
    ("/start?PROLIFIC_PID=5f0c&cohort=x", "/start?PROLIFIC_PID=[removed]&cohort=[removed]"),
])
def test_redact_query_keeps_ordinary_names_and_drops_every_value(given, logged):
    assert logsafe.redact_query(given) == logged
    assert SECRET not in logsafe.redact_query(given)


def _access_record(path: str) -> logging.LogRecord:
    """The record uvicorn's HTTP protocols log for one request (h11_impl and
    httptools_impl both: client, method, path with query, version, status)."""
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d',
                             ("203.0.113.9:0", "GET", path, "1.1", 303), None)


def test_the_filter_rewrites_uvicorns_access_record_and_never_drops_it():
    rec = _access_record(f"/rate?token={SECRET}")
    assert logsafe.StripQueryValues().filter(rec) is True
    assert SECRET not in rec.getMessage()
    assert rec.getMessage() == '203.0.113.9:0 - "GET /rate?token=[removed] HTTP/1.1" 303'


def test_the_filter_leaves_other_records_alone():
    rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started %s", ("x",), None)
    assert logsafe.StripQueryValues().filter(rec) is True
    assert rec.getMessage() == "Started x"


# --- both installations ----------------------------------------------------------

def test_the_log_config_formats_an_access_line_without_the_value(monkeypatch):
    """Through uvicorn's own formatter and handler, as configured by
    log_config(): what reaches the stream has no secret in it."""
    cfg = logsafe.log_config()
    assert cfg["handlers"]["access"]["filters"] == ["strip_query_values"]
    out = io.StringIO()
    # The same handler, writing to a buffer instead of stdout.
    cfg["handlers"]["access"].pop("class")
    cfg["handlers"]["access"]["()"] = lambda stream: logging.StreamHandler(stream)
    cfg["handlers"]["access"]["stream"] = out
    logger = logging.getLogger("uvicorn.access")
    saved = (logger.handlers[:], logger.filters[:], logger.level, logger.propagate)
    try:
        logging.config.dictConfig(cfg)
        logger.handle(_access_record(f"/api/raters?key={SECRET}"))
    finally:
        logger.handlers[:], logger.filters[:] = saved[0], saved[1]
        logger.setLevel(saved[2]); logger.propagate = saved[3]
    line = out.getvalue()
    assert "/api/raters?key=[removed]" in line, line
    assert SECRET not in line


def test_importing_the_app_puts_the_filter_on_the_access_logger():
    # server.app is imported above; install() is idempotent.
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, logsafe.StripQueryValues) for f in filters) == 1
    logsafe.install()
    assert sum(isinstance(f, logsafe.StripQueryValues) for f in logging.getLogger("uvicorn.access").filters) == 1


def test_main_starts_uvicorn_with_the_filtered_log_config(monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", RK)
    assert appmod.main() == 0
    assert seen["log_config"]["handlers"]["access"]["filters"] == ["strip_query_values"]


# --- the key as a header ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", RK)
    return TestClient(appmod.app, base_url="http://127.0.0.1", follow_redirects=False)


@pytest.mark.parametrize("path", ["/api/raters", "/api/assignments", "/api/coverage", "/api/entries", "/api/ratings"])
def test_every_researcher_route_takes_the_header(client, path):
    assert client.get(path, headers={"X-Researcher-Key": RK}).status_code == 200
    assert client.get(path, headers={"X-Researcher-Key": "wrong"}).status_code == 401
    assert client.get(path).status_code == 401
    # ?key= still works, for anything already written that way.
    assert client.get(f"{path}?key={RK}").status_code == 200


def test_the_header_wins_over_a_query_key(client):
    assert client.get("/api/raters?key=wrong", headers={"X-Researcher-Key": RK}).status_code == 200


def test_every_route_that_checks_the_key_reads_it_through_the_header_dependency():
    """A new researcher route written as `key: Optional[str] = None` would
    take the key from the URL only. Every route whose body calls check_key
    must get `key` from researcher_key."""
    import inspect
    from fastapi.params import Depends as DependsParam
    for route in appmod.app.routes:
        fn = getattr(route, "endpoint", None)
        if fn is None or "check_key(key)" not in inspect.getsource(fn):
            continue
        default = inspect.signature(fn).parameters["key"].default
        assert isinstance(default, DependsParam) and default.dependency is appmod.researcher_key, (
            f"{route.path} reads the researcher key without researcher_key()")
