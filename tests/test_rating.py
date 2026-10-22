"""Raters: sign-in by link, a blinded queue, the rating screen, one submission
per code, and the researcher side that sees the step 1 references."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

import step1_fixture
from server import app as appmod
from server import rating, source, store

RK = "rk"
SIDS = ["s_1727000000_bbbbbb", "s_1727000001_aaaaaa"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = step1_fixture.make(tmp_path / "step1")
    # a participant audio file, so the media route has something to stream
    (root / "sessions" / SIDS[0] / "user_audio.wav").write_bytes(b"RIFF" + bytes(range(256)) * 8)
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", RK)
    source.reset_source()
    store.reset_store()
    yield TestClient(appmod.app, base_url="http://127.0.0.1", follow_redirects=False)
    source.reset_source()
    store.reset_store()


def make_rater(client, rid="R-07"):
    r = client.post(f"/api/raters?key={RK}", json={"rater_id": rid, "display_name": "Seven"})
    assert r.status_code == 201, r.text
    return r.json()


def sign_in(client, token):
    r = client.get(f"/rate?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/rate"
    assert "token" not in r.headers["location"]
    assert rating.COOKIE in r.cookies
    return r


def assign(client, rid, sids):
    r = client.post(f"/api/assignments?key={RK}", json={"rater_id": rid, "session_ids": sids})
    assert r.status_code == 201, r.text
    return r.json()


BLIND = re.compile(r"s_[0-9]{1,20}_[0-9a-f]{6}|abc123|other1|0123456789ab|\bS[1-4][AB]\b|"
                   r"participant_key|run_id|cohort|scenario|Riley|Morgan|Sam\b")


def test_researcher_creates_rater_and_token_is_shown_once(client):
    out = make_rater(client)
    assert out["rater"] == {"rater_id": "R-07", "display_name": "Seven", "active": True,
                            "created_at": out["rater"]["created_at"]}
    assert out["link"].startswith("https://") and out["token"] in out["link"]
    rec = store.get_store().get("raters/R-07.json")
    assert "token" not in json.dumps(rec).replace("token_sha256", "")
    assert client.get(f"/api/raters?key={RK}").json()["raters"][0]["rater_id"] == "R-07"
    assert client.post(f"/api/raters?key={RK}", json={"rater_id": "R-07"}).status_code == 400
    assert client.post(f"/api/raters?key={RK}", json={"rater_id": "bad id!"}).status_code == 400
    assert client.post("/api/raters", json={"rater_id": "R-08"}).status_code == 401


def test_sign_in_sets_cookie_and_bad_token_does_not(client):
    out = make_rater(client)
    assert client.get("/rate").status_code == 200  # sign-in page, not the queue
    assert "Open the link" in client.get("/rate").text
    r = client.get("/rate?token=nope")
    assert r.status_code == 401 and rating.COOKIE not in r.cookies
    sign_in(client, out["token"])
    page = client.get("/rate")
    assert page.status_code == 200 and "Your queue" in page.text
    assert client.get("/api/queue").json() == {"rater_id": "R-07", "queue": [], "done": 0, "total": 0}


def test_cookie_is_signed_and_expires():
    secret = b"k" * 32
    v = rating.cookie_value("R-07", secret, now=1000.0)
    assert rating.rater_id_from_cookie(v, secret, now=2000.0) == "R-07"
    assert rating.rater_id_from_cookie(v, b"other", now=2000.0) is None
    assert rating.rater_id_from_cookie(v, secret, now=1000.0 + rating.SESSION_SECONDS + 1) is None
    assert rating.rater_id_from_cookie(v.replace("R-07", "R-08"), secret, now=2000.0) is None


def test_assignment_codes_hide_step1_and_are_idempotent(client):
    out = make_rater(client)
    res = assign(client, "R-07", SIDS)
    assert len(res["created"]) == 2 and res["already_assigned"] == [] and res["refused"] == {}
    assert all(rating.CODE_RE.fullmatch(c) for c in res["created"])
    again = assign(client, "R-07", SIDS + ["s_1727000002_cccccc"])
    assert len(again["created"]) == 1 and sorted(again["already_assigned"]) == sorted(res["created"])
    # unknown encounter and bad id are refused by name, not half-assigned
    r = client.post(f"/api/assignments?key={RK}", json={"rater_id": "R-07", "session_ids": ["s_1_999999"]}).json()
    assert r["refused"] == {"s_1_999999": "not in step 1"}
    assert client.post(f"/api/assignments?key={RK}", json={"rater_id": "R-07", "session_ids": ["nope"]}).status_code == 400
    assert client.post(f"/api/assignments?key={RK}", json={"rater_id": "R-99", "session_ids": SIDS}).status_code == 400
    # the rater's view of all this
    sign_in(client, out["token"])
    q = client.get("/api/queue")
    assert q.json()["total"] == 3 and [x["position"] for x in q.json()["queue"]] == [1, 2, 3]
    assert not BLIND.search(q.text), q.text
    # the researcher's view has the references
    a = client.get(f"/api/assignments?key={RK}&rater_id=R-07").json()["assignments"]
    assert a[0]["step1"]["session_ids"] == [SIDS[0]] and a[0]["step1"]["participant_key"] == "abc123"


def test_rating_screen_is_blind_and_items_are_all_21_in_a_fixed_order(client):
    out = make_rater(client)
    code = assign(client, "R-07", SIDS[:1])["created"][0]
    sign_in(client, out["token"])
    assert client.get(f"/rate/{code}").status_code == 200
    assert client.get("/rate/RC-0000000000").status_code == 404
    r = client.get(f"/api/rate/{code}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert not BLIND.search(r.text), r.text
    assert len(d["items"]) == 21 and len({i["key"] for i in d["items"]}) == 21
    assert d["items"] == client.get(f"/api/rate/{code}").json()["items"]  # stable
    assert [x["who"] for x in d["transcript"]] == ["Character", "Participant"]
    assert d["media"] == {"kind": "audio", "url": f"/rate/{code}/media"}
    assert d["position"] == 1 and d["total"] == 1 and d["status"] == "assigned"
    assert [s["value"] for s in d["scale"]] == [1, 2, 3, 4, 5, "na"]
    # another rater cannot see this code
    other = make_rater(client, "R-08")
    sign_in(client, other["token"])
    assert client.get(f"/api/rate/{code}").status_code == 404


def test_media_streams_with_ranges_and_hides_the_key(client):
    out = make_rater(client)
    code = assign(client, "R-07", SIDS[:1])["created"][0]
    assert client.get(f"/rate/{code}/media").status_code == 401
    sign_in(client, out["token"])
    r = client.get(f"/rate/{code}/media")
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert r.headers["cache-control"] == "no-store"
    r = client.get(f"/rate/{code}/media", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"RIFF"
    # an encounter with no media says so
    code2 = assign(client, "R-07", SIDS[1:])["created"][0]
    assert client.get(f"/api/rate/{code2}").json()["media"] is None
    assert client.get(f"/rate/{code2}/media").status_code == 404


def full_scores(items):
    return {it["key"]: (("na" if i % 7 == 0 else (i % 5) + 1)) for i, it in enumerate(items)}


def test_draft_then_submit_once(client):
    out = make_rater(client)
    code = assign(client, "R-07", SIDS[:1])["created"][0]
    sign_in(client, out["token"])
    items = client.get(f"/api/rate/{code}").json()["items"]
    # draft: partial is fine, bad values are not
    r = client.put(f"/api/rate/{code}/draft", json={"scores": {items[0]["key"]: 3}, "comments": {"better": "x"}})
    assert r.status_code == 200 and r.json() == {"saved": True, "answered": 1, "of": 21}
    assert client.put(f"/api/rate/{code}/draft", json={"scores": {items[0]["key"]: 9}}).status_code == 400
    assert client.put(f"/api/rate/{code}/draft", json={"scores": {"made_up": 1}}).status_code == 400
    d = client.get(f"/api/rate/{code}").json()["draft"]
    assert d["scores"] == {items[0]["key"]: 3} and d["comments"]["better"] == "x"
    # submit: incomplete refused, complete written once
    assert client.post(f"/api/rate/{code}/submit", json={"scores": {items[0]["key"]: 3}}).status_code == 400
    scores = full_scores(items)
    r = client.post(f"/api/rate/{code}/submit", json={"scores": scores, "comments": {"other": "fine"}})
    assert r.status_code == 201, r.text
    assert client.post(f"/api/rate/{code}/submit", json={"scores": scores}).status_code == 409
    assert client.put(f"/api/rate/{code}/draft", json={"scores": scores}).status_code == 409
    rec = store.get_store().get(f"ratings/R-07/{code}.json")
    assert rec["kind"] == "rating" and rec["step1"]["session_ids"] == [SIDS[0]]
    assert rec["step1"]["participant_key"] == "abc123" and rec["cohort"] == "study"
    assert rec["data"]["scores"] == scores and rec["data"]["comments"]["other"] == "fine"
    assert rec["data"]["media_kind"] == "audio" and len(rec["data"]["item_order"]) == 21
    assert client.get("/api/queue").json()["done"] == 1
    assert client.get(f"/api/rate/{code}").json()["status"] == "done"
    # researcher export: json and long csv
    assert client.get("/api/ratings").status_code == 401
    assert client.get(f"/api/ratings?key={RK}").json()["count"] == 1
    csv = client.get(f"/api/ratings?key={RK}&format=csv")
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("rater_id,code,session_id,participant_key") and len(lines) == 22
    assert f"R-07,{code},{SIDS[0]},abc123,0123456789ab,study," in lines[1]


def test_encounter_without_participant_key_is_refused(client, tmp_path):
    src = source.get_source()
    d = src.local_dir / "sessions" / "s_1727000009_dddddd"
    d.mkdir()
    m = step1_fixture.manifest("s_1727000009_dddddd")
    m["participant_key"] = None
    (d / "manifest.json").write_text(json.dumps(m))
    make_rater(client)
    r = client.post(f"/api/assignments?key={RK}", json={"rater_id": "R-07", "session_ids": ["s_1727000009_dddddd"]})
    assert r.status_code == 201 and list(r.json()["refused"]) == ["s_1727000009_dddddd"]
    assert r.json()["created"] == []


def test_item_order_mixes_constructs():
    order = rating.item_order("R-07", "RC-0123456789")
    keys = [r["key"] for r in order]
    from server.items import item_keys
    constructs = [item_keys()[k] for k in keys]
    # not grouped: the first five are not all one construct
    assert len(set(constructs[:5])) > 1
    assert order == rating.item_order("R-07", "RC-0123456789")
    assert order != rating.item_order("R-08", "RC-0123456789")
