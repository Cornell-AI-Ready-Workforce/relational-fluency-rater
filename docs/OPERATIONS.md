# Operations

Running the deployed rater app. The commands are bash/zsh. On Windows use Git Bash, WSL, or the PowerShell form where one is given, and type `curl.exe` rather than `curl`.

## Releasing a build

The same procedure as step 1's, with this app's names.

1. **Merge** the change to `main` by PR, with CI green.
2. **Build:** run the **build-image** workflow on `main`. It tags with the short SHA and passes `BUILD_SHA`. As a fallback, a hand build from a clean checkout of `main`:

   ```bash
   set -euo pipefail
   REGION=us-east-1
   SHA=$(git rev-parse --short HEAD)
   REGISTRY=540586745717.dkr.ecr.$REGION.amazonaws.com
   REPO=$REGISTRY/relational-fluency-rater/app
   aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$REGISTRY"
   docker build --platform linux/amd64 --build-arg BUILD_SHA=$SHA -t $REPO:$SHA .
   docker push $REPO:$SHA
   ```

   Both flags are required. Without `--platform linux/amd64`, an Apple Silicon build fails on Fargate. Without `BUILD_SHA`, `/health` reports no build and the drift check goes red. Tags are immutable, so a bad push can only be fixed by building a new commit.
3. **Pin it by PR.** The PR does one thing: it sets `container_image` in `infra/terraform/terraform.tfvars` and updates the `deployed:` line with the revision that is running now and how you checked it. Never use `tofu apply -var container_image=...`: that deploys around the committed pin.
4. **Plan through the guard**, after the pin merges:

   ```bash
   git switch main && git pull --ff-only
   tools/deploy.sh
   ```

   It refuses when:
   - the tree is dirty, or `*.auto.tfvars` or `TF_CLI_ARGS` are present
   - `origin` is not `Cornell-AI-Ready-Workforce/relational-fluency-rater`
   - HEAD is not exactly `origin/main`
   - the pin is not on main, not in ECR, or not from `relational-fluency-rater/app`
   - the service runs more than one task
   - `/health` shows `active_sessions` above 0, or cannot be read
   - the plan rolls back or deploys anything but the pin

   The overrides are `--allow-active-sessions` and `--allow-rollback`. It writes `infra/terraform/tfplan.bin` and prints the apply command. **It never applies.**
5. **Apply** what was planned, by hand: `tofu -chdir=infra/terraform apply tfplan.bin`.

## After an apply

```bash
aws ecs describe-services --cluster relational-fluency-rater --services app \
  --query 'services[0].deployments[0].rolloutState' --output text      # COMPLETED
curl -s https://rater.ai-ready-workforce.ai.cornell.edu/health          # "build" is the pinned tag
```

The **prod-build-drift** workflow runs the same comparison every morning (`python tools/check_prod_build.py`). It goes red, naming both commits, if production and main's pin disagree.

## Before every deploy: active sessions

A rollout retires the old task about two minutes after the new one is healthy. Fargate's stop timeout is capped at 120 s. This app has no long-lived connection to cut, but a participant mid-page loses their place. `/health`'s `active_sessions` counts participants seen in the last `ACTIVE_WINDOW_SECONDS` (30 minutes by default) on the task that answered. `tools/deploy.sh` refuses while it is above 0, and refuses whenever more than one task runs, because then `/health` speaks for only one of them.

```bash
curl -s https://rater.ai-ready-workforce.ai.cornell.edu/health | python -c "import json,sys; print('active sessions:', json.load(sys.stdin)['active_sessions'])"
```

## The Qualtrics link

Paste this into the survey as the link to step 2. The embedded field names must match the ones the survey uses for step 1:

```
https://rater.ai-ready-workforce.ai.cornell.edu/start?pid=${e://Field/participantId}&qid=${e://Field/ResponseID}
```

The entry follows step 1's rules:
- **A request that is not a person** (a link preview, a prefetch, a scanner, an Outlook or Slack unfurl), or one with no usable key, gets a page with a Continue button and **writes nothing**.
- **A usable key** finds the participant's step 1 encounters and writes one record. Coming back to the link resumes that record.
- **An unusable key** (unpiped `${e://...}`, a placeholder, empty) still lets the person through. They are filed as `unattributed_<hex>` in cohort `unattributed` and given the same record again if they come back with the same real Qualtrics ResponseID. The server log line `WARNING: /start got an unusable participant key` is the sign the piping is broken. Check for `unattributed` entries early in every wave.

## Looking at the data

```bash
KEY=$(aws secretsmanager get-secret-value --secret-id relational-fluency-rater/researcher-key --query SecretString --output text)
curl -s "https://rater.ai-ready-workforce.ai.cornell.edu/api/entries?key=$KEY&cohort=study"
curl -s "https://rater.ai-ready-workforce.ai.cornell.edu/api/step1/participants/PARTICIPANT_KEY/encounters?key=$KEY"
aws s3 ls s3://relational-fluency-rater-study-data/entries/
```

Every record is JSON containing:
- `step1`: `participant_key`, `session_ids`, `run_id`, `run_ids`
- `cohort`
- `build`: the commit that wrote it
- `data`

**This app cannot see step 1 withdrawals.** They live in step 1's run documents, which are not in S3; see [step1-runs-export.md](step1-runs-export.md). Until that change ships, check each participant against step 1's `/api/runs` before using their records.

## Withdrawal

Withdrawal is handled by hand, as in step 1. The bucket is versioned, so a delete must remove every version or the data stays readable:

```bash
aws s3api list-object-versions --bucket relational-fluency-rater-study-data --prefix entries/<entry_id>.json
aws s3api delete-object --bucket relational-fluency-rater-study-data --key entries/<entry_id>.json --version-id <each version>
```

To find a participant's entry, use `/api/entries`. The object name is `entries/e_<hash>.json`, derived from the participant key, so it does not contain the key itself.

## Logs

CloudWatch log group `/ecs/relational-fluency-rater/app`, kept 90 days.

```bash
aws logs tail /ecs/relational-fluency-rater/app --since 1h --follow
```
