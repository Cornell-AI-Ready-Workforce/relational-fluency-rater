"""A tiny step 1 DATA_DIR, laid out the way relational-fluency writes it."""
from __future__ import annotations

import json
from pathlib import Path


def manifest(sid, *, pk="abc123", run_id="0123456789ab", idx=1, cohort="study", scenario="S1A"):
    return {"session_id": sid, "scenario": scenario, "model": "m", "participant_id": "p_1_000000",
            "run_id": run_id, "cohort": cohort, "participant_key": pk, "encounter_index": idx,
            "status": "closed", "started_at": 1000.0 + idx, "ended_at": 2000.0, "n_turns": 4}


def record(text="hello there", hq=None):
    return {"transcript": [{"role": "agent", "text": "Hi"},
                           {"role": "participant", "text": text}],
            "participant_transcript_hq": hq}


def make(root: Path) -> Path:
    s = root / "sessions"
    rows = [("s_1727000001_aaaaaa", dict(idx=2)), ("s_1727000000_bbbbbb", dict(idx=1)),
            ("s_1727000002_cccccc", dict(pk="other1", run_id="ba9876543210"))]
    for sid, kw in rows:
        (s / sid).mkdir(parents=True)
        (s / sid / "manifest.json").write_text(json.dumps(manifest(sid, **kw)))
        (s / sid / "record.json").write_text(json.dumps(record()))
    (s / "not_a_session").mkdir()
    (root / "runs").mkdir()
    (root / "runs" / "0123456789ab.json").write_text(json.dumps({"run_id": "0123456789ab"}))
    return root
