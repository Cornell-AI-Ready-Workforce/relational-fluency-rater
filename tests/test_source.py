"""The step 1 reader: correct mapping, HQ transcript preference, and no way to write."""
from __future__ import annotations

import ast
import io
import json

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

import step1_fixture
from conftest import ROOT
from server.source import Step1Source


@pytest.fixture
def local(tmp_path):
    return Step1Source(local_dir=step1_fixture.make(tmp_path / "step1"))


def test_lists_only_real_session_ids(local):
    assert local.list_session_ids() == [
        "s_1727000000_bbbbbb", "s_1727000001_aaaaaa", "s_1727000002_cccccc"]


def test_participant_maps_to_their_encounters_in_order(local):
    got = local.encounters_for("abc123")
    assert [e["session_id"] for e in got] == ["s_1727000000_bbbbbb", "s_1727000001_aaaaaa"]
    assert {e["run_id"] for e in got} == {"0123456789ab"}
    assert local.encounters_for("nobody") == []
    assert local.encounters_for("") == []


def test_ids_are_validated_before_any_read(local):
    assert local.manifest("../../etc/passwd") is None
    assert local.manifest("S_1727000000_BBBBBB") is None
    assert local.run("../x") is None
    assert local.run("0123456789ab") == {"run_id": "0123456789ab"}


def test_transcript_prefers_offline_and_says_so(local, tmp_path):
    t = local.transcript("s_1727000000_bbbbbb")
    assert t["participant_source"] == "live" and t["participant_text"] == "hello there"
    d = tmp_path / "step1" / "sessions" / "s_1727000000_bbbbbb"
    (d / "record.json").write_text(json.dumps(step1_fixture.record(hq={"text": "from record"})))
    assert local.transcript("s_1727000000_bbbbbb")["participant_text"] == "from record"
    (d / "transcript_participant_hq.json").write_text(json.dumps({"text": "archived file", "model": "x"}))
    t = local.transcript("s_1727000000_bbbbbb")
    assert (t["participant_source"], t["participant_text"]) == ("offline", "archived file")
    assert len(t["turns"]) == 2  # the turn transcript is kept beside it


def _body(obj):
    raw = json.dumps(obj).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


def test_s3_reads_encounters_prefix_and_maps_manifests():
    client = boto3.client("s3", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)
    stub.add_response("list_objects_v2", {
        "IsTruncated": True, "NextContinuationToken": "t",
        "CommonPrefixes": [{"Prefix": "encounters/s_1727000000_bbbbbb/"},
                           {"Prefix": "encounters/_preflight/"}]},
        {"Bucket": "relational-fluency-study-data", "Prefix": "encounters/", "Delimiter": "/"})
    stub.add_response("list_objects_v2", {
        "IsTruncated": False, "CommonPrefixes": [{"Prefix": "encounters/s_1727000001_aaaaaa/"}]},
        {"Bucket": "relational-fluency-study-data", "Prefix": "encounters/", "Delimiter": "/",
         "ContinuationToken": "t"})
    for sid in ("s_1727000000_bbbbbb", "s_1727000001_aaaaaa"):
        stub.add_response("get_object", {"Body": _body(step1_fixture.manifest(sid))},
                          {"Bucket": "relational-fluency-study-data",
                           "Key": f"encounters/{sid}/manifest.json"})
    stub.add_client_error("get_object", "NoSuchKey", http_status_code=404,
                          expected_params={"Bucket": "relational-fluency-study-data",
                                           "Key": "runs/0123456789ab.json"})
    with stub:
        src = Step1Source(bucket="relational-fluency-study-data", client=client)
        assert [e["session_id"] for e in src.encounters_for("abc123")] == [
            "s_1727000000_bbbbbb", "s_1727000001_aaaaaa"]
        assert src.run("0123456789ab") is None  # step 1 does not export runs yet
    stub.assert_no_pending_responses()


WRITE_CALLS = {"put_object", "delete_object", "delete_objects", "copy_object", "upload_file",
               "upload_fileobj", "put_object_acl", "put_object_tagging", "put_bucket_policy",
               "restore_object", "create_multipart_upload", "generate_presigned_url",
               "generate_presigned_post", "write_text", "write_bytes", "unlink", "mkdir",
               "rename", "replace", "rmdir", "touch"}


def test_source_module_contains_no_write_call():
    tree = ast.parse((ROOT / "server/source.py").read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called & WRITE_CALLS), called & WRITE_CALLS
    assert not any(isinstance(n, ast.Name) and n.id == "open" for n in ast.walk(tree))


def test_read_only_wrapper_exposes_two_operations():
    from server.source import _ReadOnlyS3
    public = {n for n in vars(_ReadOnlyS3) if not n.startswith("_")}
    assert public == {"get_object", "list_objects_v2"}
