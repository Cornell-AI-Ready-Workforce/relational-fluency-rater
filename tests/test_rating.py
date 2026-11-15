"""Raters: sign-in by link, batches of four recordings the app picks, the
rating screen's data, one submission per code, and the researcher side that
sees the step 1 references."""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict

import pytest
from fastapi.testclient import TestClient

import step1_fixture
from server import app as appmod
from server import items, rating, source, store

RK = "rk"
SCENARIOS = ["S1A", "S2A", "S3B", "S4A"]


def _record(names=("riley",)):
    """Two conversations: the participant and the first character, then the second."""
    t = [{"t": 3.0, "role": "participant", "text": "Hey, Riley. What's up?", "spoken_at": 1.5},
         {"t": 8.0, "role": "agent", "agent_id": names[0], "text": "We need to talk.", "heard_at": 5.0,
          "esci": ["de_escalate"], "stage_direction": "secret", "trigger_id": "t1"},
         {"t": 20.0, "role": "participant", "text": "I'd rather talk to Sam", "interrupted": True},
         {"t": 30.0, "role": "participant", "text": ""},
         {"t": 40.0, "role": "agent", "agent_id": names[-1], "text": "", "transcript_missing": True}]
    return {"transcript": t, "cast": [{"id": n, "name": n.title()} for n in names],
            "opening": {"conversations": [{"t": 3.0}, {"t": 35.0}]}}


def make_step1(root, participants=5, *, media=True):
    """`participants` people, each with one encounter per construct."""
    s = root / "sessions"
    sids = {}
    for p in range(participants):
        pk, run = f"part{p:02d}", f"{p:012x}"
        for i, scen in enumerate(SCENARIOS):
            sid = f"s_17270{p:02d}{i:03d}_{p:02x}{i:04x}"
            d = s / sid
            d.mkdir(parents=True)
            m = step1_fixture.manifest(sid, pk=pk, run_id=run, idx=i + 1, scenario=scen)
            m["started_at"], m["ended_at"] = 1000.0, 1060.0
            (d / "manifest.json").write_text(json.dumps(m))
            rec = _record(("riley", "sam") if i == 0 else ("morgan",))
            rec["audio"] = {"participant": "user_audio.wav", "conversation": "conversation_audio.wav"}
            (d / "record.json").write_text(json.dumps(rec))
            if media:
                (d / "conversation_audio.wav").write_bytes(b"RIFF" + bytes(range(256)) * 8)
            sids[sid] = (pk, scen)
    return sids


@pytest.fixture
def step1(tmp_path, monkeypatch):
    root = tmp_path / "step1"
    sids = make_step1(root)
    monkeypatch.setenv("SOURCE_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("RATE_COHORTS", raising=False)
    source.reset_source()
    store.reset_store()
    yield root, sids
    source.reset_source()
    store.reset_store()


@pytest.fixture
def client(step1, monkeypatch):
    monkeypatch.setattr(appmod, "RESEARCHER_KEY", RK)
    return TestClient(appmod.app, base_url="http://127.0.0.1", follow_redirects=False)


def make_rater(client, rid="R-07"):
    r = client.post(f"/api/raters?key={RK}", json={"rater_id": rid, "display_name": "Seven"})
    assert r.status_code == 201, r.text
    return r.json()


def sign_in(client, token):
    r = client.get(f"/rate?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/rate"
    assert rating.COOKIE in r.cookies
    return r


def rater(client, rid="R-07"):
    sign_in(client, make_rater(client, rid)["token"])
    r = client.post("/api/batch")
    assert r.status_code == 200, r.text
    return r.json()


BLIND = re.compile(r"s_[0-9]{1,20}_[0-9a-f]{6}|part[0-9]{2}|[0-9a-f]{12}|\bS[1-4][AB]\b|"
                   r"participant_key|run_id|cohort|scenario|construct|esci|stage_direction|"
                   r"trigger|secret|Riley\b(?!\.)|Morgan|riley|morgan")


def blind(text):
    # "Hey, Riley." is the participant's own words, which the transcript must show.
    return not BLIND.search(text.replace("Hey, Riley.", ""))


# --- sign-in ---------------------------------------------------------------------------

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


def test_sign_in_serves_the_rating_screen_and_bad_token_does_not(client):
    out = make_rater(client)
    page = client.get("/rate")
    assert page.status_code == 200 and "Open the link" in page.text
    assert client.get("/api/batch").status_code == 401
    r = client.get("/rate?token=nope")
    assert r.status_code == 401 and rating.COOKIE not in r.cookies
    sign_in(client, out["token"])
    page = client.get("/rate")
    assert page.status_code == 200 and "/api/batch" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "{{build}}" not in page.text  # filled in, even when the build is unknown
    assert client.get("/api/batch").json() == {"rater_id": "R-07", "batch": None, "stages": [],
                                               "done": False, "completion_code": None}


def test_cookie_is_signed_and_expires():
    secret = b"k" * 32
    v = rating.cookie_value("R-07", secret, now=1000.0)
    assert rating.rater_id_from_cookie(v, secret, now=2000.0) == "R-07"
    assert rating.rater_id_from_cookie(v, b"other", now=2000.0) is None
    assert rating.rater_id_from_cookie(v, secret, now=1000.0 + rating.SESSION_SECONDS + 1) is None
    assert rating.rater_id_from_cookie(v.replace("R-07", "R-08"), secret, now=2000.0) is None


# --- batches -----------------------------------------------------------------------------

def test_a_batch_is_four_participants_and_stays_put_until_done(client, step1):
    _, sids = step1
    b = rater(client)
    assert b["batch"] == 1 and [s["stage"] for s in b["stages"]] == [1, 2, 3, 4]
    assert all(rating.CODE_RE.fullmatch(s["code"]) for s in b["stages"])
    assert b["done"] is False and b["completion_code"] is None
    assert blind(json.dumps(b)), b
    # asking again gives the same open batch
    assert client.post("/api/batch").json() == b and client.get("/api/batch").json() == b
    # the researcher sees which encounters they are: four different people
    a = client.get(f"/api/assignments?key={RK}&rater_id=R-07").json()["assignments"]
    assert len(a) == 4 and len({x["step1"]["participant_key"] for x in a}) == 4
    for x in a:
        sid = x["step1"]["session_ids"][0]
        assert x["step1"]["participant_key"] == sids[sid][0] and x["scenario"] == sids[sid][1]
        assert x["construct"] == items.construct_for_scenario(x["scenario"])
        assert re.fullmatch(r"RFR-[A-Z2-9]{4}-[A-Z2-9]{4}", x["completion_code"])
    assert len({x["completion_code"] for x in a}) == 1


def _finish(st, rid):
    for a in rating.list_assignments(st, rid):
        a["data"]["status"] = "done"
        st.put(rating.assignment_key(rid, a["data"]["code"]), a)


def test_allocation_rates_each_recording_twice_and_never_a_participant_twice_per_rater(step1):
    _, sids = step1
    st, src = store.get_store(), source.get_source()
    rng = random.Random(7)
    given = defaultdict(list)  # rater -> session ids
    for n in range(12):
        rid = f"R-{n:02d}"
        rating.new_rater(st, rid)
        while True:
            try:
                batch = rating.allocate_batch(st, src, rid, rng=rng)
            except rating.NoWork:
                break
            assert 1 <= len(batch) <= rating.STAGES_PER_BATCH
            given[rid] += [a["step1"]["session_ids"][0] for a in batch]
            _finish(st, rid)
    per_session = Counter(s for v in given.values() for s in v)
    assert set(per_session) == set(sids) and set(per_session.values()) == {rating.RATINGS_PER_SESSION}
    assert rating.slot_counts(st) == per_session
    for rid, got in given.items():
        assert len(got) == len(set(got)), rid
        people = [sids[s][0] for s in got]
        assert len(people) == len(set(people)), (rid, people)
    # the first batch of each rater is full while there is enough left
    assert len(given["R-00"]) == 5 and len(given["R-01"]) == 5


def test_only_eligible_encounters_are_given_out(step1, monkeypatch):
    root, sids = step1
    st, src = store.get_store(), source.get_source()
    # internal cohort, no participant key, a scenario this app doesn't know, no recording
    odd = {"s_1729000001_aaaaa1": dict(cohort="internal", pk="team01"),
           "s_1729000002_aaaaa2": dict(pk=None),
           "s_1729000003_aaaaa3": dict(scenario="missed_deadlines", pk="old001")}
    for sid, kw in odd.items():
        d = root / "sessions" / sid
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(step1_fixture.manifest(sid, **kw)))
        rec = _record(("morgan",))
        rec["audio"] = {"participant": "user_audio.wav", "conversation": "conversation_audio.wav"}
        (d / "record.json").write_text(json.dumps(rec))
        (d / "conversation_audio.wav").write_bytes(b"RIFF")
    for sid in list(sids)[:4]:
        (root / "sessions" / sid / "conversation_audio.wav").unlink()
    src.index(refresh=True)
    rating.new_rater(st, "R-01")
    got = set()
    while True:
        try:
            got |= {a["step1"]["session_ids"][0] for a in rating.allocate_batch(st, src, "R-01")}
        except rating.NoWork:
            break
        _finish(st, "R-01")
    assert got and not got & set(odd) and not got & set(list(sids)[:4])
    # a local test can add the team's own runs
    monkeypatch.setenv("RATE_COHORTS", "study,internal")
    rating.new_rater(st, "R-02")
    seen = set()
    while True:
        try:
            seen |= {a["step1"]["session_ids"][0] for a in rating.allocate_batch(st, src, "R-02")}
        except rating.NoWork:
            break
        _finish(st, "R-02")
    assert "s_1729000001_aaaaa1" in seen


def test_nothing_to_rate_is_a_409(client, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DIR", str(tmp_path / "empty"))
    source.reset_source()
    sign_in(client, make_rater(client)["token"])
    r = client.post("/api/batch")
    assert r.status_code == 409 and "no recordings" in r.json()["detail"]


# --- the rating screen ----------------------------------------------------------------------

def test_rating_screen_is_blind_and_asks_the_constructs_items(client):
    b = rater(client)
    code = b["stages"][0]["code"]
    r = client.get(f"/api/rate/{code}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert blind(r.text), r.text
    a = {x["code"]: x for x in client.get(f"/api/assignments?key={RK}").json()["assignments"]}
    construct = a[code]["construct"]
    assert sorted(i["key"] for i in d["items"]) == sorted(i["key"] for i in items.construct_items(construct))
    assert d["items"] == client.get(f"/api/rate/{code}").json()["items"]  # stable
    assert d["stage"] == 1 and d["stages"] == 4 and d["status"] == "assigned" and d["submitted"] is None
    assert d["media"] == {"kind": "audio", "url": f"/rate/{code}/media"}
    assert d["duration"] == 60.0 and d["parts"] == 2
    tr = d["transcript"]
    assert [x["part"] for x in tr] == [0, 0, 0, 0, 1]
    assert [x["t"] for x in tr] == [1.5, 5.0, 20.0, 30.0, 40.0]  # when said, else when transcribed
    assert tr[2]["note"] == "line cut off" and tr[0]["participant"] and not tr[1]["participant"]
    assert {x["who"] for x in tr} <= {"Participant", "Character", "Character 1", "Character 2"}
    assert client.get("/api/rate/RC-0000000000").status_code == 404
    # another rater cannot see this code
    sign_in(client, make_rater(client, "R-08")["token"])
    assert client.get(f"/api/rate/{code}").status_code == 404


def test_media_streams_with_ranges_and_hides_the_key(client):
    code = rater(client)["stages"][0]["code"]
    r = client.get(f"/rate/{code}/media")
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert r.headers["cache-control"] == "no-store"
    r = client.get(f"/rate/{code}/media", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"RIFF"
    client.cookies.clear()
    assert client.get(f"/rate/{code}/media").status_code == 401


def test_conversation_audio_is_preferred_to_the_participants(step1):
    root, sids = step1
    sid = next(iter(sids))
    d = root / "sessions" / sid
    rec = json.loads((d / "record.json").read_text())
    src = source.get_source()
    (d / "conversation_audio.wav").unlink()
    (d / "user_audio.wav").write_bytes(b"RIFF")
    mic = src.media_choice(sid)
    assert mic["name"] == "user_audio.wav" and mic["whole"] is False
    (d / "conversation_audio.wav").write_bytes(b"RIFF")
    mix = src.media_choice(sid)
    assert mix["name"] == "conversation_audio.wav" and mix["whole"] is True
    (d / "webcam.webm").write_bytes(b"\x1aE")
    cam = src.media_choice(sid)
    assert cam["kind"] == "video" and cam["whole"] is True


def test_mic_only_and_empty_encounters_are_not_given_out(step1):
    root, sids = step1
    st, src = store.get_store(), source.get_source()
    by_pk = {}
    for sid, (pk, _) in sids.items():
        by_pk.setdefault(pk, []).append(sid)
    pks = sorted(by_pk)
    # one person's recordings are the participant's microphone alone
    for sid in by_pk[pks[0]]:
        d = root / "sessions" / sid
        (d / "conversation_audio.wav").rename(d / "user_audio.wav")
        assert src.media_choice(sid)["name"] == "user_audio.wav" and not src.rateable(sid)
    # another's are empty: a recording, and no participant line with words
    for sid in by_pk[pks[1]]:
        d = root / "sessions" / sid
        rec = json.loads((d / "record.json").read_text())
        rec["transcript"] = [t for t in rec["transcript"] if t.get("role") not in ("participant", "user")]
        (d / "record.json").write_text(json.dumps(rec))
        assert src.media_choice(sid) and not src.rateable(sid)
    rating.new_rater(st, "R-01")
    batch = rating.allocate_batch(st, src, "R-01")
    got = {a["step1"]["participant_key"] for a in batch}
    assert len(batch) == 3 and not got & {pks[0], pks[1]}


def answers(d, score=3):
    """Every item scored; each 2-5 with the first participant line as its example."""
    keys = [i["key"] for i in d["items"]]
    return {"scores": {k: score for k in keys},
            "examples": {k: [{"line": 0}] for k in keys} if score >= 2 else {},
            "comments": {"better": "", "other": "fine"}}


def test_draft_examples_and_submit_once_in_order(client):
    b = rater(client)
    codes = [s["code"] for s in b["stages"]]
    d = client.get(f"/api/rate/{codes[0]}").json()
    k0 = d["items"][0]["key"]
    # draft: partial is fine; examples are participant lines with words, or clips
    r = client.put(f"/api/rate/{codes[0]}/draft", json={
        "scores": {k0: 3}, "examples": {k0: [{"line": 0}, {"clip": [21.5, 33]}]},
        "comments": {"better": "x"}})
    assert r.status_code == 200 and r.json() == {"saved": True, "answered": 1}
    back = client.get(f"/api/rate/{codes[0]}").json()["draft"]
    assert back["scores"] == {k0: 3} and back["examples"] == {k0: [{"line": 0}, {"clip": [21.5, 33.0]}]}
    assert back["comments"]["better"] == "x"
    # a screenshot is a moment of the video, so only a recording with a picture takes one
    shot = client.put(f"/api/rate/{codes[0]}/draft", json={"scores": {k0: 3}, "examples": {k0: [{"shot": 12.34}]}})
    if d["media"] and d["media"]["kind"] == "video":
        assert shot.status_code == 200
        assert client.get(f"/api/rate/{codes[0]}").json()["draft"]["examples"] == {k0: [{"shot": 12.3}]}
    else:
        assert shot.status_code == 400
    for bad in ({"scores": {k0: 9}}, {"scores": {"made_up": 1}},
                {"scores": {}, "examples": {k0: [{"line": 1}]}},   # a character's line
                {"scores": {}, "examples": {k0: [{"line": 3}]}},   # a participant line with no words
                {"scores": {}, "examples": {k0: [{"clip": [10, 10.5]}]}},
                {"scores": {}, "examples": {k0: [{"shot": -1}]}},
                {"scores": {}, "examples": {k0: [{"shot": "12"}]}},
                {"scores": {}, "examples": {k0: [{"shot": 3, "line": 0}]}},
                {"scores": {}, "examples": {k0: ["anything"]}}):
        assert client.put(f"/api/rate/{codes[0]}/draft", json=bad).status_code == 400, bad
    # submit: incomplete, or a 2-5 without an example, is refused
    full = answers(d)
    assert client.post(f"/api/rate/{codes[0]}/submit", json={"scores": {k0: 3}}).status_code == 400
    r = client.post(f"/api/rate/{codes[0]}/submit", json={**full, "examples": {}})
    assert r.status_code == 400 and "without an example" in r.json()["detail"]
    # in order: the second recording waits for the first
    d2 = client.get(f"/api/rate/{codes[1]}").json()
    assert client.post(f"/api/rate/{codes[1]}/submit", json=answers(d2)).status_code == 409
    r = client.post(f"/api/rate/{codes[0]}/submit", json=full)
    assert r.status_code == 201, r.text
    assert r.json()["batch_done"] is False and r.json()["completion_code"] is None
    assert client.post(f"/api/rate/{codes[0]}/submit", json=full).status_code == 409
    assert client.put(f"/api/rate/{codes[0]}/draft", json=full).status_code == 409
    done = client.get(f"/api/rate/{codes[0]}").json()
    assert done["status"] == "done" and done["submitted"]["scores"] == full["scores"]
    assert done["submitted"]["examples"] == full["examples"]
    # what was written: the step 1 references, the examples with their words
    rec = store.get_store().get(f"ratings/R-07/{codes[0]}.json")
    a = {x["code"]: x for x in client.get(f"/api/assignments?key={RK}").json()["assignments"]}
    assert rec["kind"] == "rating" and rec["step1"] == a[codes[0]]["step1"] and rec["cohort"] == "study"
    assert rec["data"]["scores"] == full["scores"] and rec["data"]["comments"]["other"] == "fine"
    assert rec["data"]["examples"][k0] == [{"line": 0, "t": 1.5, "text": "Hey, Riley. What's up?"}]
    assert rec["data"]["construct"] == a[codes[0]]["construct"] and rec["data"]["stage"] == 1
    assert rec["data"]["media_kind"] == "audio" and len(rec["data"]["item_order"]) == len(d["items"])
    # a 1 needs no example, and examples given with it are not kept
    for code in codes[1:]:
        dn = client.get(f"/api/rate/{code}").json()
        body = answers(dn, score=1)
        body["examples"] = {dn["items"][0]["key"]: [{"line": 0}]}
        last = client.post(f"/api/rate/{code}/submit", json=body)
        assert last.status_code == 201, last.text
    assert last.json()["batch_done"] is True
    code_ = last.json()["completion_code"]
    assert re.fullmatch(r"RFR-[A-Z2-9]{4}-[A-Z2-9]{4}", code_)
    assert client.get("/api/batch").json()["completion_code"] == code_
    assert store.get_store().get(f"ratings/R-07/{codes[1]}.json")["data"]["examples"] == {}
    # then a new batch, of none of the same people
    nxt = client.post("/api/batch").json()
    assert nxt["batch"] == 2 and len(nxt["stages"]) == 1  # five participants, four already rated
    # researcher export: json and long csv
    assert client.get("/api/ratings").status_code == 401
    assert client.get(f"/api/ratings?key={RK}").json()["count"] == 4
    csv = client.get(f"/api/ratings?key={RK}&format=csv")
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0] == ("rater_id,code,session_id,participant_key,run_id,cohort,construct,"
                        "item_key,score,rated_at,record_id,examples")
    s1 = a[codes[0]]["step1"]
    assert any(ln.startswith(f"R-07,{codes[0]},{s1['session_ids'][0]},{s1['participant_key']},"
                             f"{s1['run_id']},study,") and '""line"": 0' in ln for ln in lines[1:])
    cov = client.get(f"/api/coverage?key={RK}").json()
    assert cov["count"] == 20 and cov["ratings_per_session"] == 2 and cov["fully_rated"] == 0
    assert sum(r["rated"] for r in cov["sessions"]) == 4 and sum(r["given"] for r in cov["sessions"]) == 5
    assert client.get("/api/coverage").status_code == 401


def test_item_order_is_mixed_and_fixed_per_rater_and_code():
    order = rating.item_order("R-07", "RC-0123456789", "teamwork")
    keys = [r["key"] for r in order]
    assert sorted(keys) == sorted(k for k, _, _ in items.ITEMS["teamwork"])
    assert order == rating.item_order("R-07", "RC-0123456789", "teamwork")
    assert any(rating.item_order(f"R-{n:02d}", "RC-0123456789", "teamwork") != order for n in range(5))


def test_a_screenshot_is_an_example_only_when_the_recording_is_a_video():
    lines = {0: {"t": 1.0, "text": "hello"}}
    ok = rating.validate_submission({"scores": {"a": 4}, "examples": {"a": [{"shot": 7.26}]}},
                                    keys=["a"], lines=lines, complete=True, video=True)
    assert ok[1] == {"a": [{"shot": 7.3}]}
    for video, shot in ((False, 7.2), (True, True), (True, 5 * 3600)):
        with pytest.raises(rating.RatingError):
            rating.validate_submission({"scores": {"a": 4}, "examples": {"a": [{"shot": shot}]}},
                                       keys=["a"], lines=lines, complete=True, video=video)
