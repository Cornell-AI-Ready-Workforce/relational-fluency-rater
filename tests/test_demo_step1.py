"""tools/make_demo_step1.py writes data the app reads as step 1's, and only
gives out under RATE_COHORTS=internal."""
from __future__ import annotations

from server import rating, source, store
from tools import make_demo_step1


def test_demo_data_fills_a_batch(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    assert make_demo_step1.main([str(root), "--participants", "4"]) == 0
    assert make_demo_step1.main([str(root)]) == 2  # never into an existing directory
    src = source.Step1Source(local_dir=root)
    idx = src.index()
    assert len(idx) == 16 and {e["scenario"] for e in idx.values()} == {"S1A", "S2A", "S3A", "S4A"}
    sid = sorted(idx)[0]
    assert src.media_choice(sid)["name"] == "user_audio.wav"
    assert src.transcript(sid)["turns"]
    st = store.RecordStore(local_dir=tmp_path / "records")
    rating.new_rater(st, "R-01")
    monkeypatch.setenv("RATE_COHORTS", "study")
    try:
        rating.allocate_batch(st, src, "R-01")
        raise AssertionError("internal encounters given out under RATE_COHORTS=study")
    except rating.NoWork:
        pass
    monkeypatch.setenv("RATE_COHORTS", "internal")
    batch = rating.allocate_batch(st, src, "R-01")
    assert len(batch) == 4 and len({a["step1"]["participant_key"] for a in batch}) == 4
