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
# deployed: relational-fluency-rater-app:2 carries b6d3777, the first release
#   on the rebuilt stack (pin #30), applied 2026-11-12. Checked that day:
#   rollout COMPLETED with one task, target healthy, and
#   https://rater.ai-ready-workforce.ai.cornell.edu/health answers
#   "build":"b6d3777". The image does not change in this commit; only
#   rate_cohorts below does, which makes a new task-definition revision.
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency-rater/app:b6d3777"

# Retention: rater_data_retention_days defaults to 0 = no expiration rule. It is
# the PI's decision; to expire records, set a positive number here, in a commit:
#
#   rater_data_retention_days = ...

# Which step 1 cohorts raters are given. TESTING VALUE: "internal" is the
# team's own test runs (15 usable recordings on 2026-11-12, audio only), set so
# the site can be tried before step 1 has study sessions. Set it back to
# "study" (or delete the line) before real raters are created.
rate_cohorts = "internal"
