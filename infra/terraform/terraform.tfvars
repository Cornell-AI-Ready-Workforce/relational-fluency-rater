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
# deployed: nothing yet; this is the FIRST release. Base stack applied
#   2026-11-08 (41 resources; cluster relational-fluency-rater has 0 services,
#   no task definition exists), researcher key set. This tag (cbed27e, main
#   with #2 and #3) is built with BUILD_SHA by build-image run 42536229322 and
#   pushed, awaiting the apply that registers relational-fluency-rater-app:1.
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency-rater/app:cbed27e"

# Retention: rater_data_retention_days defaults to 0 = no expiration rule. It is
# the PI's decision; to expire records, set a positive number here, in a commit:
#
#   rater_data_retention_days = ...
