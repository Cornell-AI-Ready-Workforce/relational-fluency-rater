# Everything this stack uses but does not own. Step 1's stack
# (relational-fluency, state key platform/terraform.tfstate) owns these; here
# they are DATA SOURCES ONLY: never `import`ed, never declared as resources,
# so no plan of this stack can change or destroy them.
# tests/test_terraform.py fails on an import block or a resource of these types
# naming them.

data "aws_caller_identity" "current" {}

data "aws_s3_bucket" "step1" {
  bucket = var.step1_bucket
}

data "aws_kms_alias" "step1" {
  name = var.step1_kms_alias
}

data "aws_route53_zone" "main" {
  name = var.domain_name
}
