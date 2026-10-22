"""Static checks on infra/terraform: the promises the stack makes, kept.

No credentials, no network: these read the .tf files. `tofu validate` runs in
CI (.github/workflows/ci.yml); `tofu plan` only ever runs through tools/deploy.sh.
"""
from __future__ import annotations

import re

import pytest

from conftest import ROOT
from server.store import STEP1_BUCKET, WRITE_PREFIXES

TF = ROOT / "infra" / "terraform"
ALL = {p.name: p.read_text(encoding="utf-8") for p in sorted(TF.glob("*.tf"))}
TEXT = "\n".join(ALL.values())
TFVARS = (TF / "terraform.tfvars").read_text(encoding="utf-8")


def block(kind: str, *labels: str) -> str:
    head = re.escape(kind) + "".join(r'\s+"' + re.escape(l) + '"' for l in labels) + r"\s*\{"
    m = re.search(head, TEXT)
    assert m, f"no {kind} {' '.join(labels)}"
    depth, i = 0, m.end() - 1
    for j in range(i, len(TEXT)):
        depth += {"{": 1, "}": -1}.get(TEXT[j], 0)
        if depth == 0:
            return TEXT[m.start():j + 1]
    raise AssertionError("unbalanced braces")


def resources():
    return re.findall(r'(?m)^resource\s+"([^"]+)"\s+"([^"]+)"', TEXT)


def test_own_state_key_in_shared_backend():
    b = block("backend", "s3")
    assert 'bucket         = "relational-fluency-tfstate-540586745717"' in b
    assert 'dynamodb_table = "relational-fluency-tflock"' in b
    assert re.search(r'key\s*=\s*"rater/terraform\.tfstate"', b)
    assert "platform/" not in b


def test_project_prefix_and_hostname():
    assert 'default     = "relational-fluency-rater"' in block("variable", "project")
    assert 'default     = "rater"' in block("variable", "app_subdomain")


def test_step1_resources_are_data_sources_only():
    assert not re.search(r"(?m)^\s*import\s*\{", TEXT), "never import step 1's resources"
    for kind, name in [("aws_s3_bucket", "step1"), ("aws_kms_alias", "step1"),
                       ("aws_route53_zone", "main")]:
        assert block("data", kind, name)
    # No managed resource is built on a step 1 name or reference...
    for kind, name in resources():
        body = block("resource", kind, name)
        if kind.startswith("aws_s3_bucket") or kind.startswith("aws_kms") or kind == "aws_route53_zone":
            assert "step1" not in body, f"{kind}.{name} manages something of step 1's"
    assert ("aws_route53_zone", "main") not in resources()
    # ...and step 1's own resource names never appear as literals in a resource.
    for kind, name in resources():
        body = block("resource", kind, name)
        assert STEP1_BUCKET not in body
        assert '"relational-fluency/' not in body and 'relational-fluency-task' not in body


def test_ecr_immutable():
    ecr = block("resource", "aws_ecr_repository", "app")
    assert 'image_tag_mutability = "IMMUTABLE"' in ecr
    assert 'name                 = "${var.project}/app"' in ecr


@pytest.mark.parametrize("kind,name", [("aws_s3_bucket", "rater_data"), ("aws_kms_key", "rater")])
def test_prevent_destroy(kind, name):
    assert re.search(r"prevent_destroy\s*=\s*true", block("resource", kind, name))


def test_bucket_built_like_step1():
    assert "enable_key_rotation = true" in block("resource", "aws_kms_key", "rater")
    assert 'status = "Enabled"' in block("resource", "aws_s3_bucket_versioning", "rater_data")
    sse = block("resource", "aws_s3_bucket_server_side_encryption_configuration", "rater_data")
    assert 'sse_algorithm     = "aws:kms"' in sse and "aws_kms_key.rater.arn" in sse
    pab = block("resource", "aws_s3_bucket_public_access_block", "rater_data")
    assert pab.count("= true") == 4
    assert "aws:SecureTransport" in block("data", "aws_iam_policy_document", "rater_data_bucket")
    # No CORS: the browser never uploads directly.
    assert "aws_s3_bucket_cors_configuration" not in TEXT


def test_retention_defaults_to_zero_and_is_prefix_scoped():
    assert re.search(r"default\s*=\s*0\b", block("variable", "rater_data_retention_days"))
    lc = block("resource", "aws_s3_bucket_lifecycle_configuration", "rater_data")
    assert "for_each = local.rater_write_prefixes" in lc
    assert "precondition" in lc
    assert "rater_data_retention_days > 0 ? [1] : []" in lc


def test_write_prefixes_match_the_app():
    m = re.search(r"rater_write_prefixes\s*=\s*\[([^\]]*)\]", TEXT)
    assert m
    assert tuple(re.findall(r'"([^"]+)"', m.group(1))) == WRITE_PREFIXES


ALLOWED_ON_STEP1 = {"s3:GetObject", "s3:ListBucket", "kms:Decrypt"}


def _statements(doc: str):
    for m in re.finditer(r"statement\s*\{", doc):
        depth, j = 0, m.end() - 1
        for k in range(j, len(doc)):
            depth += {"{": 1, "}": -1}.get(doc[k], 0)
            if depth == 0:
                yield doc[m.start():k + 1]
                break


def test_step1_is_read_only_for_the_task():
    doc = block("data", "aws_iam_policy_document", "task_step1_read")
    allowed, denied = set(), set()
    for st in _statements(doc):
        acts = set(re.findall(r'"((?:s3|kms):[A-Za-z*]+)"', st.split("resources")[0]))
        (denied if re.search(r'effect\s*=\s*"Deny"', st) else allowed).update(acts)
    assert allowed == ALLOWED_ON_STEP1, allowed
    assert {"s3:PutObject*", "s3:DeleteObject*"} <= denied
    assert "kms:ViaService" in doc
    assert "s3:prefix" in doc
    # No other policy document may touch the step 1 bucket.
    for name in re.findall(r'data\s+"aws_iam_policy_document"\s+"([^"]+)"', TEXT):
        if name != "task_step1_read":
            assert "aws_s3_bucket.step1" not in block("data", "aws_iam_policy_document", name), name
            assert "step1_kms" not in block("data", "aws_iam_policy_document", name), name


def test_task_definition_matches_the_image():
    td = block("resource", "aws_ecs_task_definition", "app")
    assert 'user         = "1000:1000"' in td
    assert "readonlyRootFilesystem = true" in td
    assert 'cpu_architecture        = "X86_64"' in td
    assert "stopTimeout = 120" in td
    assert '{ name = "RESEARCHER_KEY", valueFrom = aws_secretsmanager_secret.researcher_key.arn }' in td
    assert 'name        = "${var.project}/researcher-key"' in block(
        "resource", "aws_secretsmanager_secret", "researcher_key")


def test_no_service_without_an_image():
    for kind in ("aws_ecs_task_definition", "aws_ecs_service"):
        assert 'count                    = var.container_image != "" ? 1 : 0' in block("resource", kind, "app") \
            or 'count           = var.container_image != "" ? 1 : 0' in block("resource", kind, "app")


def test_tfvars_pin_and_deployed_line():
    assert re.search(r"(?m)^# deployed: \S", TFVARS), "terraform.tfvars needs a `deployed:` line"
    m = re.findall(r'(?m)^\s*container_image\s*=\s*"([^"]*)"', TFVARS)
    assert len(m) == 1
    if m[0]:
        from tools import pinned_image
        pin = pinned_image.parse(TFVARS)
        assert pin.repository == "relational-fluency-rater/app"
