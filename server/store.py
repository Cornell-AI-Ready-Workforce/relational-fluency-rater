"""This app's own records: s3://relational-fluency-rater-study-data.

Everything step 2 saves goes here and nowhere else. Three rules, each enforced
in code and mirrored in infra/terraform:

1. NEVER THE STEP 1 BUCKET. RecordStore refuses to be built on
   relational-fluency-study-data or on whatever SOURCE_BUCKET names. The IAM
   policy denies it too; this is the second lock on the same door.
2. ONLY UNDER WRITE_PREFIXES. infra/terraform/storage_secrets.tf scopes the
   lifecycle (retention) rules and the task role's PutObject to the same list
   (local.rater_write_prefixes), and tests/test_terraform.py checks that the
   two lists are equal. A prefix written here that Terraform does not know
   about would be data no retention rule reaches.
3. EVERY RECORD POINTS BACK AT STEP 1. new_record() requires the step 1
   participant_key, and carries the session_ids and run_id the record was
   based on, each validated against step 1's own shapes (server/conventions.py).

Objects are encrypted by the bucket's default SSE-KMS key; the code sends no
encryption headers, so it cannot pick a different key by mistake.

Locally (RATER_BUCKET unset) records go under DATA_DIR/rater-records/, with
the same keys, so nothing a laptop does reaches the production bucket unless
someone sets RATER_BUCKET on purpose.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .build_info import build_sha
from .conventions import (
    normalize_cohort, valid_run_id, valid_session_id, valid_stored_participant_key,
)

STEP1_BUCKET = "relational-fluency-study-data"
# entries/      the Qualtrics entry records (the participant path)
# raters/       who may rate, and the digest of their link token
# assignments/  rating code -> step 1 encounter, per rater
# drafts/       a rater's autosaved answers, overwritten as they go
# ratings/      submissions, written once                   (server/rating.py)
WRITE_PREFIXES = ("entries/", "raters/", "assignments/", "drafts/", "ratings/")
SCHEMA_VERSION = 1


class StoreError(ValueError):
    pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_key(key: str) -> None:
    if not any(key.startswith(p) for p in WRITE_PREFIXES):
        raise StoreError(f"refusing to write {key!r}: not under {', '.join(WRITE_PREFIXES)}")
    if ".." in key.split("/") or key.endswith("/") or "//" in key:
        raise StoreError(f"refusing to write {key!r}: not a plain object key")


def step1_refs(participant_key: str, encounters: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """The step 1 references for a record, from source.encounters_for() rows."""
    rows = list(encounters)
    run_ids: List[str] = []
    for r in rows:
        rid = r.get("run_id")
        if rid and rid not in run_ids:
            run_ids.append(rid)
    return {
        "participant_key": participant_key,
        "session_ids": [r["session_id"] for r in rows],
        # The run the encounters belong to: the latest if a participant has
        # more than one (all of them are in run_ids). None when step 1 holds no
        # encounters for this key.
        "run_id": run_ids[-1] if run_ids else None,
        "run_ids": run_ids,
    }


def validate_step1(refs: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(refs, dict):
        raise StoreError("step1 references are required")
    pk = refs.get("participant_key")
    if not valid_stored_participant_key(pk):
        raise StoreError(f"step1.participant_key {pk!r} is not a step 1 participant key")
    sids = refs.get("session_ids")
    if not isinstance(sids, list) or not all(valid_session_id(s) for s in sids):
        raise StoreError("step1.session_ids must be a list of step 1 session ids")
    run_ids = refs.get("run_ids", [])
    if not isinstance(run_ids, list) or not all(valid_run_id(r) for r in run_ids):
        raise StoreError("step1.run_ids must be a list of step 1 run ids")
    rid = refs.get("run_id")
    if rid is not None and not valid_run_id(rid):
        raise StoreError(f"step1.run_id {rid!r} is not a step 1 run id")
    if sids and rid is None:
        raise StoreError("step1.run_id is required when the record is based on encounters")
    return {"participant_key": pk, "session_ids": list(sids), "run_id": rid,
            "run_ids": list(run_ids or ([rid] if rid else []))}


def new_record(kind: str, *, step1: Dict[str, Any], cohort: str,
               data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "record_id": f"{kind}_{int(time.time())}_{secrets.token_hex(4)}",
        "created_at": _now_iso(),
        "build": build_sha(),
        "cohort": normalize_cohort(cohort),
        "step1": validate_step1(step1),
        "data": data or {},
    }


class RecordStore:
    def __init__(self, *, bucket: str = "", local_dir: Optional[Path] = None,
                 client=None, source_bucket: str = ""):
        if bucket and bucket in {STEP1_BUCKET, source_bucket}:
            raise StoreError(f"refusing to write to {bucket!r}: that is step 1's bucket, "
                             f"which this app may only read")
        if not bucket and local_dir is None:
            raise StoreError("RecordStore needs RATER_BUCKET or a local directory")
        self.bucket = bucket
        self.local_dir = Path(local_dir).resolve() if (local_dir and not bucket) else None
        if bucket and client is None:
            from .source import _s3_client
            client = _s3_client()
        self._client = client

    @property
    def kind(self) -> str:
        return "s3" if self.bucket else "local"

    def _local_path(self, key: str) -> Path:
        assert self.local_dir is not None
        return self.local_dir / key

    def put(self, key: str, obj: Dict[str, Any], *, create_only: bool = False) -> bool:
        """Write one JSON object. With create_only, False when it already exists."""
        _check_key(key)
        if obj.get("kind") and "step1" in obj:
            validate_step1(obj["step1"])
        body = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if self.bucket:
            from botocore.exceptions import ClientError
            kw: Dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": body,
                                  "ContentType": "application/json"}
            if create_only:
                kw["IfNoneMatch"] = "*"
            try:
                self._client.put_object(**kw)
            except ClientError as exc:
                if create_only and exc.response.get("Error", {}).get("Code") in (
                        "PreconditionFailed", "ConditionalRequestConflict"):
                    return False
                raise
            return True
        path = self._local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            if create_only:
                try:
                    os.link(tmp, path)  # atomic, and fails if the target exists
                except FileExistsError:
                    return False
            else:
                os.replace(tmp, path)
                tmp = ""
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        return True

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        _check_key(key)
        if self.bucket:
            from botocore.exceptions import ClientError
            try:
                raw = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    return None
                raise
        else:
            p = self._local_path(key)
            if not p.is_file():
                return None
            raw = p.read_bytes()
        return json.loads(raw.decode("utf-8"))

    def list_keys(self, prefix: str) -> List[str]:
        if not any(prefix.startswith(p) for p in WRITE_PREFIXES):
            raise StoreError(f"{prefix!r} is not one of this app's prefixes")
        if self.bucket:
            keys, token = [], None
            while True:
                kw: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
                if token:
                    kw["ContinuationToken"] = token
                page = self._client.list_objects_v2(**kw)
                keys += [o["Key"] for o in page.get("Contents") or []]
                if not page.get("IsTruncated"):
                    return keys
                token = page.get("NextContinuationToken")
        root = self.local_dir
        assert root is not None
        base = root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(root)).replace(os.sep, "/")
                      for p in base.rglob("*.json") if not p.name.startswith(".tmp-"))


_store: Optional[RecordStore] = None


def data_dir() -> Path:
    return Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")


def get_store() -> RecordStore:
    global _store
    if _store is None:
        bucket = os.getenv("RATER_BUCKET", "").strip()
        _store = RecordStore(bucket=bucket,
                             local_dir=None if bucket else data_dir() / "rater-records",
                             source_bucket=os.getenv("SOURCE_BUCKET", "").strip())
    return _store


def reset_store() -> None:
    global _store
    _store = None
