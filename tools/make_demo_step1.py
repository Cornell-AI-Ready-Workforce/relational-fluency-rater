"""Made-up step 1 data for trying the rating screen locally.

Step 1's own local data is usually one or two team test runs, too few people
to fill a batch of four recordings from four different participants. This
writes PARTICIPANTS made-up people, each with four encounters (S1A, S2A, S3A,
S4A, one per construct), in step 1's local layout (sessions/<id>/manifest.json,
record.json and a recording), for SOURCE_DIR. The conversations are the UI
prototype's four examples (tools/demo_conversations.json). The recording is a
quiet WAV with a short tone wherever the participant starts a line, so a seek
from the transcript can be checked by ear; --video gives every encounter a copy
of a webm of your own instead.

    python -m tools.make_demo_step1 data/demo-step1
    SOURCE_DIR=data/demo-step1 RATE_COHORTS=internal python -m server.app

The encounters are in cohort "internal" (step 1's cohort for the team's own
runs), so they are given out only when RATE_COHORTS names it. data/ is
gitignored.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import sys
import wave
from pathlib import Path

CONVERSATIONS = Path(__file__).resolve().parent / "demo_conversations.json"
RATE = 8000
EPOCH = 1800000000


def write_wav(path: Path, duration: float, beeps) -> None:
    """Silence, with a 0.25 s tone at each time in `beeps`."""
    n = int(duration * RATE)
    samples = bytearray(n * 2)
    for t in beeps:
        start = int(t * RATE)
        for i in range(start, min(n, start + RATE // 4)):
            struct.pack_into("<h", samples, i * 2, int(3000 * math.sin(2 * math.pi * 660 * i / RATE)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(samples))


def _place(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)  # the same file for every participant: no need for twenty copies
    except OSError:
        shutil.copyfile(src, dst)


def make(root: Path, participants: int, video: Path | None = None) -> list:
    convs = json.loads(CONVERSATIONS.read_text(encoding="utf-8"))
    media_dir = root / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    wavs = {}
    for c in convs:
        wavs[c["scenario"]] = media_dir / f"{c['scenario']}.wav"
        write_wav(wavs[c["scenario"]], c["duration"],
                  [t["t"] for t in c["record"]["transcript"] if t["role"] == "participant"])
    made = []
    for p in range(participants):
        pk, run_id = f"demo{p + 1:02d}", f"de{p:010x}"
        for i, c in enumerate(convs):
            sid = f"s_{EPOCH + p * 10 + i}_de{p:02x}{i:02x}"
            d = root / "sessions" / sid
            d.mkdir(parents=True)
            started = float(EPOCH + p * 10 + i)
            manifest = {"session_id": sid, "scenario": c["scenario"], "participant_id": f"p_{EPOCH}_{p:06x}",
                        "run_id": run_id, "cohort": "internal", "participant_key": pk,
                        "encounter_index": i + 1, "status": "closed", "started_at": started,
                        "ended_at": started + c["duration"], "n_turns": len(c["record"]["transcript"])}
            record = {"encounter_id": sid, "scenario": c["scenario"], "participant_key": pk,
                      "run_id": run_id, "cohort": "internal", **c["record"],
                      "audio": {"participant": "user_audio.wav"}}
            (d / "manifest.json").write_text(json.dumps(manifest, indent=1))
            (d / "record.json").write_text(json.dumps(record, indent=1, ensure_ascii=False))
            _place(wavs[c["scenario"]], d / "user_audio.wav")
            if video:
                _place(video, d / "webcam.webm")
            made.append(sid)
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", type=Path, help="where to write, e.g. data/demo-step1 (must not exist yet)")
    ap.add_argument("--participants", type=int, default=6)
    ap.add_argument("--video", type=Path, help="a .webm to use as every encounter's webcam recording")
    a = ap.parse_args(argv)
    if a.root.exists():
        print(f"{a.root} already exists; choose a new directory (this tool only writes made-up data "
              f"into a fresh one).", file=sys.stderr)
        return 2
    if a.video and not a.video.is_file():
        print(f"no such file: {a.video}", file=sys.stderr)
        return 2
    made = make(a.root, max(1, a.participants), a.video)
    print(f"Wrote {len(made)} encounters for {a.participants} made-up participants under {a.root}.\n"
          f"Run:  SOURCE_DIR={a.root} RATE_COHORTS=internal python -m server.app")
    return 0


if __name__ == "__main__":
    sys.exit(main())
