"""The Qualtrics entry: step 1's rules, and one record per participant that
names their step 1 encounters."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import step1_fixture
from server import app as appmod
from server import source, store

BROWSER = {"user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605 Safari/605"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DIR", str(step1_fixture.make(tmp_path / "step1")))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", "rk")
    source.reset_source()
    store.reset_store()
    yield TestClient(appmod.app, base_url="http://127.0.0.1", follow_redirects=False)
    source.reset_source()
    store.reset_store()


def entries():
    s = store.get_store()
    return [s.get(k) for k in s.list_keys("entries/")]


def test_participant_entry_writes_one_record_naming_step1(client):
    r = client.get("/start?pid=abc123&qid=R_abcdefgh1234", headers=BROWSER)
    assert r.status_code == 303 and r.headers["location"].startswith("/visit?v=e_")
    [rec] = entries()
    assert rec["cohort"] == "study"
    assert rec["step1"] == {"participant_key": "abc123",
                            "session_ids": ["s_1727000000_bbbbbb", "s_1727000001_aaaaaa"],
                            "run_id": "0123456789ab", "run_ids": ["0123456789ab"]}
    assert rec["data"]["qualtrics_id"] == "R_abcdefgh1234"
    assert rec["data"]["raw_participant_key"] is None
    # Returning is resuming, not a second record.
    assert client.get("/start?pid=abc123", headers=BROWSER).headers["location"] == r.headers["location"]
    assert len(entries()) == 1
    assert client.get(r.headers["location"]).status_code == 200
    assert client.get("/health").json()["active_sessions"] >= 1


@pytest.mark.parametrize("spelling", ["pid", "participant_id", "participantId", "PROLIFIC_PID"])
def test_all_step1_key_spellings(client, spelling):
    assert client.get(f"/start?{spelling}=abc123", headers=BROWSER).status_code == 303


@pytest.mark.parametrize("headers", [
    {"user-agent": ""}, {"user-agent": "Slackbot-LinkExpanding 1.0"},
    {**BROWSER, "sec-purpose": "prefetch"}, {"user-agent": "Microsoft Office/16.0 (Outlook)"},
])
def test_link_fetchers_get_the_check_page_and_write_nothing(client, headers):
    r = client.get("/start?pid=abc123", headers=headers)
    assert r.status_code == 200 and "Continue" in r.text
    assert 'name="pid" value="abc123"' in r.text
    assert entries() == []


@pytest.mark.parametrize("pid", ["", "${e://Field/participantId}", "undefined", "ab"])
def test_unusable_key_check_page_then_unattributed(client, pid):
    r = client.get(f"/start?pid={pid}&qid=R_abcdefgh1234", headers=BROWSER)
    assert r.status_code == 200 and entries() == []
    r = client.post("/start", data={"pid": pid, "qid": "R_abcdefgh1234"}, headers=BROWSER)
    assert r.status_code == 303
    [rec] = entries()
    assert rec["cohort"] == "unattributed"
    assert rec["step1"]["participant_key"].startswith("unattributed_")
    assert rec["step1"]["session_ids"] == []
    # Same survey response, pressed again: same record.
    client.post("/start", data={"pid": pid, "qid": "R_abcdefgh1234"}, headers=BROWSER)
    assert len(entries()) == 1


def test_cohort_override_needs_researcher_key(client):
    client.get("/start?pid=abc123&cohort=internal", headers=BROWSER)
    assert entries()[0]["cohort"] == "study"
    client.get("/start?pid=other1&cohort=internal&key=rk", headers=BROWSER)
    assert {e["step1"]["participant_key"]: e["cohort"] for e in entries()}["other1"] == "internal"
    assert client.get("/start?pid=zzz999&cohort=banana&key=rk", headers=BROWSER).status_code == 400


def test_html_injection_in_check_page_is_escaped(client):
    r = client.get('/start?pid="><script>x</script>', headers={})
    assert "<script>x" not in r.text


def test_visit_rejects_unknown_and_malformed(client):
    assert client.get("/visit?v=e_" + "0" * 24).status_code == 404
    assert client.get("/visit?v=../../x").status_code == 404


def test_step1_unreachable_is_503_and_writes_nothing(client, monkeypatch):
    def boom(_):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(source.get_source(), "encounters_for", boom)
    assert client.get("/start?pid=abc123", headers=BROWSER).status_code == 503
    assert entries() == []


def test_researcher_api_is_keyed(client):
    client.get("/start?pid=abc123", headers=BROWSER)
    assert client.get("/api/entries").status_code == 401
    assert client.get("/api/entries?key=rk").json()["count"] == 1
    r = client.get("/api/step1/participants/abc123/encounters?key=rk").json()
    assert [e["encounter_index"] for e in r["encounters"]] == [1, 2]
    assert client.get("/api/step1/participants/abc123/encounters").status_code == 401
