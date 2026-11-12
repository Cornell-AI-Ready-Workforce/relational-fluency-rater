# Deployed release. Committed on purpose, as in relational-fluency: the running
# version is then visible in git history, and a plain `tofu apply` cannot fall
# back to something else.
#
# THE PIN IS THE TRUTH OR IT IS A ROLLBACK. The `deployed:` line below names
# the ECS task-definition revision this tag was checked against, and
# tests/test_terraform.py requires it. Change the tag and say what you verified.
#
# To release a build (docs/OPERATIONS.md, "Releasing a build"):
#   1. the build-image workflow on main (or docker build --platform linux/amd64
#      --build-arg BUILD_SHA=$SHA -t $REPO:$SHA . && docker push $REPO:$SHA)
#   2. by PR: set container_image below to $REPO:$SHA and update `deployed:`
#   3. after it merges: tools/deploy.sh from an up-to-date main (it plans; it
#      never applies)
#   4. tofu -chdir=infra/terraform apply tfplan.bin, then check /health "build"
#
# deployed: nothing yet; this is the FIRST release on the rebuilt stack. The
#   stack of 2026-11-08 (cbed27e as relational-fluency-rater-app:1) was
#   destroyed that evening. On 2026-11-12 the base stack was applied again
#   from main b6d3777 with container_image = "": 41 resources, no task
#   definition and no service (checked: `tofu state list`, and ECS shows no
#   service on cluster relational-fluency-rater). The researcher-key secret is
#   the 2026-11-08 one, restored from pending deletion and imported, with its
#   value intact; the KMS key and the bucket are new. b6d3777 was built by
#   build-image run 43173382945 and is in ECR relational-fluency-rater/app.
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency-rater/app:b6d3777"

# Retention: rater_data_retention_days defaults to 0 = no expiration rule. It is
# the PI's decision; to expire records, set a positive number here, in a commit:
#
#   rater_data_retention_days = ...
