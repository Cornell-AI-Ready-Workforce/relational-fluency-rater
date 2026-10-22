resource "aws_ecs_cluster" "main" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project}/app"
  retention_in_days = 90 # same as step 1
}

# --- IAM -------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.project}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.researcher_key.arn]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.project}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

locals {
  step1_kms_key_arn = data.aws_kms_alias.step1.target_key_arn
}

# STEP 1, READ ONLY. GetObject under the read prefixes, ListBucket limited to
# the same prefixes, and Decrypt on step 1's key through S3 only. Then an
# explicit Deny of every write, delete and permission change on the bucket: an
# Allow added to this role later, by anyone, for any reason, still cannot
# override it. tests/test_terraform.py fails if a write-shaped action is ever
# allowed on the step 1 bucket.
data "aws_iam_policy_document" "task_step1_read" {
  statement {
    sid       = "ReadStep1Objects"
    actions   = ["s3:GetObject"]
    resources = [for p in var.step1_read_prefixes : "${data.aws_s3_bucket.step1.arn}/${p}*"]
  }
  statement {
    sid       = "ListStep1Prefixes"
    actions   = ["s3:ListBucket"]
    resources = [data.aws_s3_bucket.step1.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = flatten([for p in var.step1_read_prefixes : [p, "${p}*"]])
    }
  }
  statement {
    sid       = "DecryptStep1ViaS3"
    actions   = ["kms:Decrypt"]
    resources = [local.step1_kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
  statement {
    sid    = "NeverWriteStep1"
    effect = "Deny"
    actions = [
      "s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:AbortMultipartUpload",
      "s3:ReplicateObject", "s3:PutBucket*", "s3:DeleteBucket*", "s3:PutLifecycleConfiguration",
      "s3:PutEncryptionConfiguration", "s3:PutReplicationConfiguration",
    ]
    resources = [data.aws_s3_bucket.step1.arn, "${data.aws_s3_bucket.step1.arn}/*"]
  }
  statement {
    sid       = "NeverEncryptWithStep1Key"
    effect    = "Deny"
    actions   = ["kms:Encrypt", "kms:GenerateDataKey*", "kms:ReEncrypt*", "kms:CreateGrant"]
    resources = [local.step1_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "task_step1_read" {
  name   = "step1-read-only"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_step1_read.json
}

# This app's own bucket: read and write under its prefixes, nothing else.
data "aws_iam_policy_document" "task_rater_data" {
  statement {
    sid       = "ReadWriteRecords"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [for p in local.rater_write_prefixes : "${aws_s3_bucket.rater_data.arn}/${p}*"]
  }
  statement {
    sid       = "ListRecordPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.rater_data.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = flatten([for p in local.rater_write_prefixes : [p, "${p}*"]])
    }
  }
  statement {
    sid       = "UseRaterKey"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.rater.arn]
  }
}

resource "aws_iam_role_policy" "task_rater_data" {
  name   = "rater-data-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_rater_data.json
}

# --- task definition & service ----------------------------------------------------------
#
# Both exist only once container_image is set. Before the first image is pushed
# there is nothing to run, and a service pointed at a missing tag would
# crash-loop (docs/DEPLOY-AWS.md, "First deploy").

resource "aws_ecs_task_definition" "app" {
  count                    = var.container_image != "" ? 1 : 0
  family                   = "${var.project}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # Stated, unlike step 1's, so an arm64 image fails at registration rather
  # than as an exec-format crash loop.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name         = "app"
    image        = var.container_image
    essential    = true
    user         = "1000:1000" # the Dockerfile's appuser, same uid as step 1
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    # Nothing in production writes to local disk (server/store.py writes to S3).
    readonlyRootFilesystem = true
    environment = [
      { name = "APP_HOST", value = local.app_fqdn },
      { name = "AWS_REGION", value = var.region },
      { name = "SOURCE_BUCKET", value = data.aws_s3_bucket.step1.bucket },
      { name = "RATER_BUCKET", value = aws_s3_bucket.rater_data.bucket },
      { name = "HOST", value = "0.0.0.0" },
      { name = "PORT", value = "8080" },
    ]
    secrets = [
      { name = "RESEARCHER_KEY", valueFrom = aws_secretsmanager_secret.researcher_key.arn },
    ]
    # Fargate's hard cap. A rollout retires the old task about two minutes
    # after the new one is healthy; tools/deploy.sh checks /health first.
    stopTimeout = 120
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "app"
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  count           = var.container_image != "" ? 1 : 0
  name            = "app"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app[0].arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = module.vpc.public_subnets
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true # no NAT; see network.tf
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8080
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  lifecycle {
    ignore_changes = [desired_count]
  }
}
