# Built the way relational-fluency@f9947d2:infra/terraform/storage_secrets.tf
# builds step 1's study bucket: a customer-managed KMS key with rotation,
# versioning, prevent_destroy on the bucket and the key, public access blocked,
# and lifecycle rules scoped by prefix with retention a variable defaulting to
# 0. The reasons given there apply here unchanged and are not all repeated.
#
# No CORS configuration: the browser never uploads to this bucket. Every
# object is written by the task (server/store.py).

# --- ECR ------------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = "${var.project}/app"
  image_tag_mutability = "IMMUTABLE" # release-SHA images; audit which version ran

  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- KMS --------------------------------------------------------------------------------

resource "aws_kms_key" "rater" {
  description         = "${var.project} study data (step 2 records)"
  enable_key_rotation = true

  # Scheduling this key for deletion makes every object in the bucket
  # permanently unreadable once the waiting period ends. Refused at plan time.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "rater" {
  name          = "alias/${var.project}-study"
  target_key_id = aws_kms_key.rater.key_id
}

# --- the bucket ------------------------------------------------------------------------

# The prefixes the app writes (server/store.py WRITE_PREFIXES). One list, read
# by the task role's PutObject grant (ecs.tf) AND by the retention rules below,
# so the data the app can write and the data retention reaches cannot drift
# apart. tests/test_terraform.py checks it equals WRITE_PREFIXES.
locals {
  rater_write_prefixes = ["entries/"]
}

resource "aws_s3_bucket" "rater_data" {
  bucket = "${var.project}-study-data"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "rater_data" {
  bucket = aws_s3_bucket.rater_data.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "rater_data" {
  bucket = aws_s3_bucket.rater_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rater_data" {
  bucket = aws_s3_bucket.rater_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.rater.arn
    }
    # Fewer KMS calls per object, same key. The app sends no encryption headers
    # (server/store.py), so this default is what every object gets.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "rater_data" {
  bucket                  = aws_s3_bucket.rater_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# TLS only. Step 1's docs list this as planned for its bucket; it is here
# from the start.
data "aws_iam_policy_document" "rater_data_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.rater_data.arn,
      "${aws_s3_bucket.rater_data.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "rater_data" {
  bucket     = aws_s3_bucket.rater_data.id
  policy     = data.aws_iam_policy_document.rater_data_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.rater_data]
}

# --- retention ---------------------------------------------------------------------------
#
# THE PERIOD IS THE PI'S DECISION, as for step 1 (where it was decided on
# 2026-09-17 not to expire recordings). 0, the default, means no expiration
# rule: records are kept until somebody deletes them. A positive number makes
# S3 delete them on that schedule, so it is set deliberately, never guessed.

variable "rater_data_retention_days" {
  description = "Days a step 2 record is kept before S3 expires it. 0 (the default) means no expiration rule."
  type        = number
  default     = 0

  validation {
    condition     = var.rater_data_retention_days >= 0
    error_message = "rater_data_retention_days must be 0 (keep indefinitely) or a positive number of whole days."
  }
}

# On a versioned bucket `expiration` only writes a delete marker; the old
# version stays, readable, until a noncurrent-version rule removes it. This is
# that second clock: an operational recovery window, not the retention period.
variable "rater_data_superseded_version_days" {
  description = "Days a superseded (noncurrent) version survives after being replaced. A recovery window, not the retention period."
  type        = number
  default     = 30

  validation {
    condition     = var.rater_data_superseded_version_days >= 1
    error_message = "rater_data_superseded_version_days must be at least 1 day."
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "rater_data" {
  bucket     = aws_s3_bucket.rater_data.id
  depends_on = [aws_s3_bucket_versioning.rater_data]

  lifecycle {
    # A superseded copy that outlives the retention period is the promise to
    # the participant broken by a second timer nobody is watching.
    precondition {
      condition     = var.rater_data_retention_days == 0 || var.rater_data_superseded_version_days <= var.rater_data_retention_days
      error_message = "rater_data_superseded_version_days must not exceed rater_data_retention_days."
    }
  }

  # Every rule that DELETES DATA is filtered to a prefix the app writes, never
  # bucket-wide, so it cannot reach an export or a manual copy an operator puts
  # here by hand.
  dynamic "rule" {
    for_each = local.rater_write_prefixes
    content {
      id     = "retention-${trimsuffix(rule.value, "/")}"
      status = "Enabled"

      filter {
        prefix = rule.value
      }

      dynamic "expiration" {
        for_each = var.rater_data_retention_days > 0 ? [1] : []
        content {
          days = var.rater_data_retention_days
        }
      }

      noncurrent_version_expiration {
        noncurrent_days = var.rater_data_superseded_version_days
      }

      abort_incomplete_multipart_upload {
        days_after_initiation = 7
      }
    }
  }

  # Bucket-wide on purpose: it removes only delete markers with nothing left
  # under them, so it cannot delete data by construction.
  rule {
    id     = "expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }
}

# --- secrets ------------------------------------------------------------------------------
#
# Values are set OUT OF BAND (aws secretsmanager put-secret-value), never in
# Terraform state or git. ECS will not start a task whose secret has no value,
# so set it before the first apply that creates the service.

resource "aws_secretsmanager_secret" "researcher_key" {
  name        = "${var.project}/researcher-key"
  description = "RESEARCHER_KEY: gates every researcher route of the rater app."
}
