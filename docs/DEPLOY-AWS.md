# Deploying to AWS

This app deploys the way step 1 ([relational-fluency](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency)) does:
- an OpenTofu stack in [`infra/terraform/`](../infra/terraform/), using the same S3 state backend under its own key
- an image tagged with the short git SHA in an IMMUTABLE ECR repository
- the tag pinned by PR in `terraform.tfvars`, with a `deployed:` verification line
- `tools/deploy.sh`, which plans and never applies
- the apply, typed by a person

Account `540586745717`, region `us-east-1`.

## What the stack owns, and what it only reads

| | Owned by this stack | Read only (data source) |
|---|---|---|
| State | `s3://relational-fluency-tfstate-540586745717/rater/terraform.tfstate` | the same bucket and lock table `relational-fluency-tflock`, created by step 1's `add-deployer.sh` |
| Network | VPC `10.41.0.0/16`: 2 public subnets, no NAT, S3 gateway endpoint, ALB and its security groups | none |
| DNS / TLS | ACM certificate and A record for `rater.ai-ready-workforce.ai.cornell.edu` | Route 53 zone `ai-ready-workforce.ai.cornell.edu` |
| Images | ECR `relational-fluency-rater/app` (IMMUTABLE, scan on push) | none |
| Data | `relational-fluency-rater-study-data` and KMS `alias/relational-fluency-rater-study` (rotation on, `prevent_destroy` on both) | `relational-fluency-study-data` and `alias/relational-fluency-study` |
| Compute | ECS cluster `relational-fluency-rater`, service `app`, task family `relational-fluency-rater-app`, log group `/ecs/relational-fluency-rater/app` | none |
| IAM | roles `relational-fluency-rater-task` and `relational-fluency-rater-task-execution` | none |
| Secrets | `relational-fluency-rater/researcher-key` | none |

Nothing of step 1's is `import`ed or declared as a resource. `tests/test_terraform.py` fails if that changes, and also if the state key is ever anything but `rater/terraform.tfstate`.

**Why its own VPC and ALB.**
- **Isolation.** Sharing step 1's ALB would mean adding a listener rule and a certificate to the listener that serves participants, so a priority or certificate mistake here could break collection. Sharing its VPC would tie this stack to step 1's subnet IDs and NAT.
- **Cost.** About $16/month for the ALB and about $11 for public IPv4 addresses. There is no NAT gateway: tasks sit in public subnets with a public IP, their security group admits only the ALB, and outbound traffic is HTTPS only.

**The task role's access to step 1:**
- `s3:GetObject` on `encounters/*` and `runs/*`
- `s3:ListBucket` limited by `s3:prefix` to those two prefixes
- `kms:Decrypt` on step 1's key, only through S3 (`kms:ViaService`)
- an explicit **Deny** of every S3 write, delete or bucket-configuration change on that bucket, and of encryption with its key

An Allow added later cannot override that Deny.

## Install

| | macOS | Windows | Linux |
|---|---|---|---|
| OpenTofu | `brew install opentofu` | `winget install --id=OpenTofu.Tofu -e` | your distro's package, or opentofu.org |
| AWS CLI v2 | `brew install awscli` | `winget install -e --id Amazon.AWSCLI` | the AWS installer |
| Docker | Docker Desktop | Docker Desktop | Docker Engine |

`tools/deploy.sh` is bash. On Windows, run it from Git Bash or WSL.

## What the deploy identities need

Step 1 has no dedicated deploy role. Each deployer is an IAM user created by step 1's `infra/scripts/add-deployer.sh`, with `PowerUserAccess` plus an inline policy `relational-fluency-deployer`. `PowerUserAccess` already covers everything this stack needs except IAM: ECR, S3 (including the new state key), DynamoDB, ACM, Route 53, ELB, ECS, Secrets Manager, KMS and CloudWatch.

The gaps:

1. **Creating and deleting the two new roles.** The inline policy allows `iam:PutRolePolicy`, `AttachRolePolicy` and related actions on `role/relational-fluency-*`, which also matches `relational-fluency-rater-*`. It does not allow `iam:CreateRole` or `iam:DeleteRole`, so **the first apply needs an administrator.** Later applies only change those roles' policies, which deployers can already do.
2. **`iam:PassRole` for the new roles.** The inline policy names exactly `relational-fluency-task` and `relational-fluency-task-execution`. Registering this app's task definition needs both of these, with the same `iam:PassedToService = ecs-tasks.amazonaws.com` condition:

   ```
   arn:aws:iam::540586745717:role/relational-fluency-rater-task
   arn:aws:iam::540586745717:role/relational-fluency-rater-task-execution
   ```

   The change belongs in step 1's `add-deployer.sh`, then re-run for each deployer.
3. **The GitHub OIDC role for `build-image`.** Step 1's OIDC role was made by hand and is not in its repository. Its trust policy almost certainly limits `token.actions.githubusercontent.com:sub` to `repo:Cornell-AI-Ready-Workforce/relational-fluency:*`, and its ECR rights are probably scoped to `relational-fluency/platform`. Recommended: a **new** role for this repository, set as this repo's `AWS_ROLE_ARN` secret, that can:
   - be assumed only by `repo:Cornell-AI-Ready-Workforce/relational-fluency-rater:ref:refs/heads/main`
   - call `ecr:GetAuthorizationToken` on `*`
   - call `ecr:BatchCheckLayerAvailability`, `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`, `BatchGetImage` and `DescribeImages` on `arn:aws:ecr:us-east-1:540586745717:repository/relational-fluency-rater/app`

   Without `DescribeImages`, the workflow's existence probe refuses to build rather than guess.
4. **Check before the first apply; don't assume:**
   - Does the state bucket have a bucket policy that limits object keys? Read-only check: `aws s3api get-bucket-policy --bucket relational-fluency-tfstate-540586745717`. If it does, add `rater/*`.
   - Does step 1's KMS key policy delegate to IAM? The Terraform default does, and if so the task role's `kms:Decrypt` grant is enough. Check with `aws kms get-key-policy --key-id alias/relational-fluency-study --policy-name default`. If the policy does not include the account root principal, step 1's key policy needs to allow `relational-fluency-rater-task`, which is a change in step 1's stack.

## First deploy

Nothing has been applied yet. With `container_image = ""` the stack creates everything except the task definition and the service. The order:

1. **An administrator applies the base stack.** IAM role creation is the reason it has to be an administrator. From a clean, up-to-date `main`:

   ```bash
   tofu -chdir=infra/terraform init
   tofu -chdir=infra/terraform plan -out tfplan.bin    # read it: everything is a create; nothing of step 1's appears
   tofu -chdir=infra/terraform apply tfplan.bin
   ```

   `tools/deploy.sh` refuses to plan with an empty pin, on purpose: there is no build to check yet. This is the one plan made without it.
2. **Set the researcher key.** ECS will not start a task whose secret has no value.

   ```bash
   aws secretsmanager put-secret-value --secret-id relational-fluency-rater/researcher-key \
     --secret-string "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
   ```
3. **Grant the deploy identities** what the section above lists, including `AWS_ROLE_ARN` in this repository's secrets.
4. **Build the image:** run the `build-image` workflow on `main`, or follow the hand build in [OPERATIONS.md](OPERATIONS.md#releasing-a-build).
5. **Pin it by PR.** Set `container_image` to `540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency-rater/app:<sha>` and change the `deployed:` line to say this is the first release and nothing ran before it.
6. **Plan through the guard**, after the PR merges:

   ```bash
   git switch main && git pull --ff-only
   tools/deploy.sh --allow-active-sessions
   ```

   The flag is needed this once: there is no service to count tasks on and no `/health` to ask.
7. **Apply and verify:** `tofu -chdir=infra/terraform apply tfplan.bin`, then follow [OPERATIONS.md, "After an apply"](OPERATIONS.md#after-an-apply).

## Destroying

Don't destroy this stack. The bucket and the KMS key have `prevent_destroy`, so `tofu destroy` stops at plan time. Deleting the key would make every record unreadable once its waiting period ends.
