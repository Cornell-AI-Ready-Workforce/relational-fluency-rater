"""Step 1's data, read-only.

Step 1 (relational-fluency) archives every closed encounter to

    s3://relational-fluency-study-data/encounters/<session_id>/
        manifest.json  record.json  events.jsonl  *.wav  webcam.webm
        transcript_participant_hq.json   (once step 1 archives it; see below)

under the KMS key alias/relational-fluency-study, and keeps run documents
(order, arm, Qualtrics id, withdrawal) on its own EFS volume only. So a
participant is mapped to their encounters by SCANNING MANIFESTS: every
manifest carries participant_key, run_id, cohort, encounter_index and
scenario. If step 1 starts exporting runs to runs/<run_id>.json
(docs/step1-runs-export.md), run() below reads them; until then it answers
None and nothing here pretends to know about withdrawals.

READ-ONLY BY CONSTRUCTION. The S3 client is wrapped in _ReadOnlyS3, which calls
exactly two S3 operations, GetObject (whole, or streamed with a Range for
media) and ListObjectsV2; there is no handle in this
module through which a write could be made, and tests/test_source.py fails on
any write-shaped call appearing in this file. The IAM policy (ecs.tf) grants
the same two, and denies writes outright.

Locally, SOURCE_DIR points at a copy of step 1's DATA_DIR (sessions/<id>/ and
runs/<run_id>.json); with neither SOURCE_BUCKET nor SOURCE_DIR set there is no
step 1 data and every lookup is empty.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .conventions import valid_run_id, valid_session_id

ENCOUNTERS_PREFIX = "encounters/"
RUNS_PREFIX = "runs/"
HQ_TRANSCRIPT = "transcript_participant_hq.json"
INDEX_TTL_SECONDS = int(os.getenv("SOURCE_INDEX_TTL_SECONDS", "300"))


class _ReadOnlyS3:
    """The only S3 operations this module can perform."""

    def __init__(self, client, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    def get_object(self, key: str, *, stream: bool = False,
                   range_header: Optional[str] = None) -> Optional[Any]:
        """The object's bytes, or None when it is missing.

        With stream=True, the raw GetObject response instead, so a media file
        (a webcam recording is tens of MB) can be sent in pieces and a
        browser's Range request honoured; then {"status": 416} means the range
        was not satisfiable. Still the one S3 operation."""
        from botocore.exceptions import ClientError
        kw: Dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if range_header:
            kw["Range"] = range_header
        try:
            resp = self._client.get_object(**kw)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            if stream and code == "InvalidRange":
                return {"status": 416}
            raise
        return resp if stream else resp["Body"].read()

    def list_objects_v2(self, **kwargs) -> Dict[str, Any]:
        return self._client.list_objects_v2(Bucket=self.bucket, **kwargs)



def _s3_client():
    import boto3
    from botocore.config import Config

    # Same client settings as step 1's video.py: SigV4 (the bucket is
    # KMS-encrypted) and short timeouts, so a black-holed S3 path costs
    # seconds rather than a hung request.
    return boto3.client(
        "s3", region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=10,
                      retries={"mode": "standard", "max_attempts": 3}))


class Step1Source:
    def __init__(self, *, bucket: str = "", local_dir: Optional[Path] = None, client=None):
        self.bucket = bucket
        self.local_dir = Path(local_dir).resolve() if local_dir else None
        self._s3 = _ReadOnlyS3(client or _s3_client(), bucket) if bucket else None
        self._index: Optional[Dict[str, Dict[str, Any]]] = None
        self._index_at = 0.0
        self._lock = threading.Lock()

    @property
    def kind(self) -> str:
        return "s3" if self._s3 else ("local" if self.local_dir else "none")

    # --- raw reads -------------------------------------------------------------------

    def _read(self, key: str) -> Optional[bytes]:
        """One object by its S3 key, e.g. encounters/<sid>/manifest.json."""
        if self._s3:
            return self._s3.get_object(key)
        if self.local_dir:
            # Step 1's local layout: sessions/<sid>/... and runs/<run_id>.json.
            rel = ("sessions/" + key[len(ENCOUNTERS_PREFIX):]
                   if key.startswith(ENCOUNTERS_PREFIX) else key)
            p = (self.local_dir / rel).resolve()
            if self.local_dir not in p.parents or not p.is_file():
                return None
            return p.read_bytes()
        return None

    def _json(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self._read(key)
        if raw is None:
            return None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def list_session_ids(self) -> List[str]:
        out: List[str] = []
        if self._s3:
            token = None
            while True:
                kw = {"Prefix": ENCOUNTERS_PREFIX, "Delimiter": "/"}
                if token:
                    kw["ContinuationToken"] = token
                page = self._s3.list_objects_v2(**kw)
                for cp in page.get("CommonPrefixes") or []:
                    sid = cp["Prefix"][len(ENCOUNTERS_PREFIX):].rstrip("/")
                    if valid_session_id(sid):
                        out.append(sid)
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        elif self.local_dir and (self.local_dir / "sessions").is_dir():
            out = [p.name for p in (self.local_dir / "sessions").iterdir()
                   if p.is_dir() and valid_session_id(p.name)]
        return sorted(out)

    # --- per encounter --------------------------------------------------------------

    def manifest(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not valid_session_id(session_id):
            return None
        return self._json(f"{ENCOUNTERS_PREFIX}{session_id}/manifest.json")

    def record(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not valid_session_id(session_id):
            return None
        return self._json(f"{ENCOUNTERS_PREFIX}{session_id}/record.json")

    def run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The run document, once step 1 exports runs (docs/step1-runs-export.md)."""
        if not valid_run_id(run_id):
            return None
        return self._json(f"{RUNS_PREFIX}{run_id}.json")

    def transcript(self, session_id: str) -> Dict[str, Any]:
        """The transcript to use, preferring step 1's offline re-transcription.

        The offline version (retranscribe.py, a stronger model on the
        participant's audio) is participant-only text, not turn-aligned, so it
        is returned BESIDE the live turn transcript rather than instead of it.
        `participant_source` says which one the participant text came from, and
        a record built on it should store that.
        """
        rec = self.record(session_id) or {}
        hq = self._json(f"{ENCOUNTERS_PREFIX}{session_id}/{HQ_TRANSCRIPT}") \
            if valid_session_id(session_id) else None
        if not (isinstance(hq, dict) and (hq.get("text") or "").strip()):
            hq = rec.get("participant_transcript_hq")
        has_hq = isinstance(hq, dict) and bool((hq.get("text") or "").strip())
        turns = rec.get("transcript") if isinstance(rec.get("transcript"), list) else []
        return {
            "session_id": session_id,
            "participant_source": "offline" if has_hq else "live",
            "participant_text": (hq["text"] if has_hq else "\n".join(
                str(t.get("text") or "") for t in turns if t.get("role") == "participant")),
            "offline": hq if has_hq else None,
            "turns": turns,
        }

    # --- media -------------------------------------------------------------------------
    #
    # What a rater watches or listens to. The webcam recording when the browser
    # uploaded one (webcam.webm), else the participant's own audio channel
    # (record.json audio.participant, user_audio.wav in every archive so far).
    # The characters' audio is per agent in separate files and is not mixed
    # here; the transcript carries their lines.

    VIDEO_NAME = "webcam.webm"

    def media_files(self, session_id: str) -> Dict[str, int]:
        """file name -> size, for one encounter's folder."""
        if not valid_session_id(session_id):
            return {}
        out: Dict[str, int] = {}
        if self._s3:
            prefix = f"{ENCOUNTERS_PREFIX}{session_id}/"
            token = None
            while True:
                kw: Dict[str, Any] = {"Prefix": prefix}
                if token:
                    kw["ContinuationToken"] = token
                page = self._s3.list_objects_v2(**kw)
                for o in page.get("Contents") or []:
                    out[o["Key"][len(prefix):]] = int(o.get("Size") or 0)
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        elif self.local_dir:
            d = self.local_dir / "sessions" / session_id
            if d.is_dir():
                out = {p.name: p.stat().st_size for p in d.iterdir() if p.is_file()}
        return out

    def media_choice(self, session_id: str) -> Optional[Dict[str, Any]]:
        """{"name", "kind": video|audio, "content_type", "size"} or None."""
        files = self.media_files(session_id)
        if self.VIDEO_NAME in files:
            return {"name": self.VIDEO_NAME, "kind": "video", "content_type": "video/webm",
                    "size": files[self.VIDEO_NAME]}
        rec = self.record(session_id) or {}
        wanted = ((rec.get("audio") or {}).get("participant")) or "user_audio.wav"
        if wanted in files and wanted.endswith(".wav"):
            return {"name": wanted, "kind": "audio", "content_type": "audio/wav",
                    "size": files[wanted]}
        return None

    def media_local_path(self, session_id: str, name: str) -> Optional[Path]:
        """The file on disk (local source only), for a range-capable file response."""
        if not self.local_dir or not valid_session_id(session_id) or "/" in name or name.startswith("."):
            return None
        p = (self.local_dir / "sessions" / session_id / name).resolve()
        return p if self.local_dir in p.parents and p.is_file() else None

    def media_stream(self, session_id: str, name: str, range_header: Optional[str] = None):
        """S3 only: the raw get_object response for streaming, or None / {"status": 416}."""
        if not self._s3 or not valid_session_id(session_id) or "/" in name:
            return None
        return self._s3.get_object(f"{ENCOUNTERS_PREFIX}{session_id}/{name}", stream=True,
                                   range_header=range_header)

    # --- participant -> encounters --------------------------------------------------

    def _summary(self, sid: str, m: Dict[str, Any]) -> Dict[str, Any]:
        return {k: m.get(k) for k in (
            "session_id", "run_id", "participant_key", "participant_id", "cohort",
            "encounter_index", "scenario", "status", "started_at", "ended_at", "n_turns")
        } | {"session_id": sid}

    def index(self, *, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """session_id -> manifest summary, for every archived encounter."""
        with self._lock:
            fresh = self._index is not None and time.time() - self._index_at < INDEX_TTL_SECONDS
            if fresh and not refresh:
                return self._index  # type: ignore[return-value]
            idx: Dict[str, Dict[str, Any]] = {}
            for sid in self.list_session_ids():
                m = self.manifest(sid)
                if m is not None:
                    idx[sid] = self._summary(sid, m)
            self._index, self._index_at = idx, time.time()
            return idx

    def encounters_for(self, participant_key: str) -> List[Dict[str, Any]]:
        """A participant's encounters, in the order they had them."""
        if not participant_key:
            return []
        found = [e for e in self.index().values() if e.get("participant_key") == participant_key]
        return sorted(found, key=lambda e: (e.get("encounter_index") or 0, e.get("started_at") or 0))


_source: Optional[Step1Source] = None


def get_source() -> Step1Source:
    global _source
    if _source is None:
        bucket = os.getenv("SOURCE_BUCKET", "").strip()
        local = os.getenv("SOURCE_DIR", "").strip()
        _source = Step1Source(bucket=bucket, local_dir=Path(local) if (local and not bucket) else None)
    return _source


def reset_source() -> None:
    global _source
    _source = None
