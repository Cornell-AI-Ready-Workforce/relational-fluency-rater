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
# deployed: nothing. The stack was applied and cbed27e ran as
#   relational-fluency-rater-app:1 on 2026-11-08, then the whole stack was
#   destroyed the same evening (43 resources; the team decided the rater UI is
#   not to run as a site of its own). State rater/terraform.tfstate is empty;
#   the KMS key and the researcher-key secret are pending deletion; the ECR
#   images and the GitHub build role were deleted. A new deploy starts again
#   from docs/DEPLOY-AWS.md, "First deploy".
container_image = ""

# Retention: rater_data_retention_days defaults to 0 = no expiration rule. It is
# the PI's decision; to expire records, set a positive number here, in a commit:
#
#   rater_data_retention_days = ...
