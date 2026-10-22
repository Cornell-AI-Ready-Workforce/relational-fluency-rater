# infra

`terraform/` is this app's OpenTofu stack. It is separate from step 1's and lives in the same S3 state backend under the key `rater/terraform.tfstate`.

| File | What it holds |
|---|---|
| `versions.tf` | provider pin, the backend (shared bucket, own key), default tags |
| `variables.tf` | project prefix `relational-fluency-rater`, hostname, image pin, the step 1 names read |
| `data_step1.tf` | everything owned by step 1's stack: study bucket, its KMS alias, the Route 53 zone. Data sources only |
| `network.tf` | own VPC `10.41.0.0/16`, public subnets, no NAT, S3 gateway endpoint, security groups |
| `alb.tf` | ALB, target group on `/health`, HTTPS listener (TLS 1.3 policy), HTTP redirect |
| `dns_tls.tf` | ACM certificate and alias record for `rater.ai-ready-workforce.ai.cornell.edu` |
| `storage_secrets.tf` | ECR (IMMUTABLE), KMS key, the rater bucket and its retention, the researcher-key secret |
| `ecs.tf` | cluster, logs, IAM roles (step 1 read-only with an explicit write Deny), task definition, service |
| `outputs.tf` | URLs, bucket, ECR repository, roles |
| `terraform.tfvars` | the pinned `container_image` and its `deployed:` line |

Plans go through `tools/deploy.sh`. Applies are typed by a person. The first deploy, and the IAM the deploy identities need, are in [`docs/DEPLOY-AWS.md`](../docs/DEPLOY-AWS.md).
