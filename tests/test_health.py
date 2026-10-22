from fastapi.testclient import TestClient

from server import app as appmod


def client():
    return TestClient(appmod.app, base_url="http://127.0.0.1")


def test_health_shape_and_build(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "abc1234")
    body = client().get("/health").json()
    assert body["status"] == "ok"
    assert body["build"] == "abc1234"
    assert isinstance(body["active_sessions"], int)


def test_health_is_exempt_from_host_allowlist():
    c = TestClient(appmod.app, base_url="http://10.0.3.7")
    assert c.get("/health").status_code == 200
    assert c.get("/static/favicon.png").status_code == 400


def test_active_sessions_window(monkeypatch):
    reg = appmod.ActivityRegistry()
    monkeypatch.setattr(appmod, "ACTIVE_WINDOW_SECONDS", 60)
    reg.touch("v1")
    assert reg.active() == 1
    assert reg.active(now=10**12) == 0


def test_csp_forbids_third_parties():
    csp = client().get("/health").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "http" not in csp


def test_key_comparison_is_constant_time_and_closed_when_set(monkeypatch):
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", "s3cret")
    assert appmod.is_researcher("s3cret")
    assert not appmod.is_researcher("s3cre")
    assert not appmod.is_researcher(None)


def test_refuses_public_start_without_key(monkeypatch):
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", "")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", ["rater.ai-ready-workforce.ai.cornell.edu"])
    assert "RESEARCHER_KEY is empty" in appmod._refuse_unprotected_public_start()
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", [])
    assert "any Host header" in appmod._refuse_unprotected_public_start()
    monkeypatch.setenv("HOST", "127.0.0.1")
    assert appmod._refuse_unprotected_public_start() is None
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", "k")
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert appmod._refuse_unprotected_public_start() is None


def test_public_start_needs_both_buckets(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", ["rater.ai-ready-workforce.ai.cornell.edu"])
    assert "SOURCE_BUCKET and RATER_BUCKET" in appmod._refuse_public_start_without_buckets()
    monkeypatch.setenv("SOURCE_BUCKET", "relational-fluency-study-data")
    monkeypatch.setenv("RATER_BUCKET", "relational-fluency-rater-study-data")
    assert appmod._refuse_public_start_without_buckets() is None
    monkeypatch.delenv("RATER_BUCKET")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
    assert appmod._refuse_public_start_without_buckets() is None  # docker run locally
