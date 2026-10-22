"""The writer: own bucket only, own prefixes only, every record tied to step 1."""
from __future__ import annotations

import boto3
import pytest
from botocore.stub import ANY, Stubber

from server import store as st
from server.store import RecordStore, StoreError, new_record, step1_refs

ENC = [{"session_id": "s_1727000000_bbbbbb", "run_id": "0123456789ab"},
       {"session_id": "s_1727000001_aaaaaa", "run_id": "0123456789ab"}]


def test_refuses_step1_bucket():
    for b in ("relational-fluency-study-data",):
        with pytest.raises(StoreError, match="step 1's bucket"):
            RecordStore(bucket=b, client=object())
    with pytest.raises(StoreError, match="step 1's bucket"):
        RecordStore(bucket="some-copy", client=object(), source_bucket="some-copy")


def test_record_carries_step1_references():
    rec = new_record("entry", step1=step1_refs("abc123", ENC), cohort="study")
    assert rec["step1"] == {"participant_key": "abc123",
                            "session_ids": ["s_1727000000_bbbbbb", "s_1727000001_aaaaaa"],
                            "run_id": "0123456789ab", "run_ids": ["0123456789ab"]}
    assert rec["cohort"] == "study" and rec["schema"] == 1


@pytest.mark.parametrize("bad", [
    {},
    {"participant_key": "a/b", "session_ids": []},
    {"participant_key": "abc123", "session_ids": ["S_1_AAAAAA"]},
    {"participant_key": "abc123", "session_ids": ["s_1_aaaaaa"], "run_id": None},
    {"participant_key": "abc123", "session_ids": [], "run_id": "nope"},
])
def test_bad_references_are_refused(bad):
    with pytest.raises(StoreError):
        new_record("entry", step1=bad, cohort="study")


def test_no_encounters_is_allowed_and_explicit():
    rec = new_record("entry", step1=step1_refs("abc123", []), cohort="unattributed")
    assert rec["step1"]["session_ids"] == [] and rec["step1"]["run_id"] is None


def test_local_create_only_and_prefixes(tmp_path):
    s = RecordStore(local_dir=tmp_path)
    rec = new_record("entry", step1=step1_refs("abc123", ENC), cohort="study")
    assert s.put("entries/a.json", rec, create_only=True) is True
    assert s.put("entries/a.json", rec, create_only=True) is False
    assert s.get("entries/a.json")["step1"]["participant_key"] == "abc123"
    assert s.list_keys("entries/") == ["entries/a.json"]
    for key in ("encounters/x.json", "runs/x.json", "entries/../x.json", "entries/"):
        with pytest.raises(StoreError):
            s.put(key, rec)


def test_s3_put_is_conditional_and_sends_no_encryption_headers():
    client = boto3.client("s3", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)
    stub.add_response("put_object", {}, {"Bucket": "relational-fluency-rater-study-data",
                                         "Key": "entries/a.json", "Body": ANY,
                                         "ContentType": "application/json", "IfNoneMatch": "*"})
    stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    with stub:
        s = RecordStore(bucket="relational-fluency-rater-study-data", client=client)
        rec = new_record("entry", step1=step1_refs("abc123", ENC), cohort="study")
        assert s.put("entries/a.json", rec, create_only=True) is True
        assert s.put("entries/a.json", rec, create_only=True) is False
    stub.assert_no_pending_responses()


def test_get_store_defaults_to_local(monkeypatch, tmp_path):
    st.reset_store()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert st.get_store().kind == "local"
    st.reset_store()
