# Proposed change to step 1: export runs to S3

**For a PR in [relational-fluency](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency). Not made from this repository.**

## Why

The rater app matches a participant to their encounters by scanning `encounters/<session_id>/manifest.json` in the study bucket. That works today: every manifest carries `participant_key`, `run_id`, `cohort` and `encounter_index`.

What the manifests do not carry lives only in step 1's run documents, on its EFS volume (`/data/runs/<run_id>.json`) and behind `/api/runs`:
- **withdrawal**
- the Williams order row and arm
- the Qualtrics ResponseID (`qualtrics_id`, `qualtrics_ids_seen`)
- exits

Without these, the rater app cannot tell that a participant withdrew. It also cannot be given access to EFS or `/api/runs` without either sharing step 1's network and filesystem or holding step 1's `SESSION_KEY`, and neither is a good trade.

Two related gaps found while reading step 1 (at `f9947d2`):
- `transcript_participant_hq.json`, the offline re-transcription, is not in `archive.py`'s `ARCHIVE_FILES`. It reaches S3 only inside `record.json`, and only if the session is archived again after `retranscribe.py` runs.
- A webcam recording saved through the server fallback (`PUT /api/sessions/{id}/video`, stored as `sessions/<id>/webcam.*`) is never uploaded. Only a salvaged stream copy is.

## The change

1. **`server/runs.py`: mirror each run document to S3.** After every atomic write of `runs/<run_id>.json` (create, `advance`, `withdraw`, exits, Qualtrics-id updates), upload the same bytes to `s3://relational-fluency-study-data/runs/<run_id>.json`.
   - Use a best-effort daemon thread, the way `archive.py` uploads encounters. A failed upload is logged and never fails the participant's request.
   - Upload the file just written, not a re-serialisation, so EFS and S3 hold identical bytes.
   - Respect the same `ARCHIVE_SESSIONS_TO_S3` switch, so a local checkout with it off uploads nothing.
2. **Backfill at startup.** On boot, alongside `sweep_streams()`, upload any run document whose S3 copy is missing or older than the EFS file. That covers runs created before the change and any upload that failed.
3. **`server/archive.py`: archive the offline transcript and the fallback video.** Add `transcript_participant_hq.json` to `ARCHIVE_FILES` and `webcam.*` to `ARCHIVE_GLOBS`. In `retranscribe.py`, re-archive the session after writing the transcript.
4. **`infra/terraform/ecs.tf`: let the task write the new prefix.** Add `"${aws_s3_bucket.study_data.arn}/runs/*"` to the task role's `PutObject`/`GetObject` statement.
5. **`infra/terraform/storage_secrets.tf`: retention for `runs/`.** Add a `runs-retention` rule on the same clock as `encounters/` (`study_data_retention_days`, `study_data_superseded_version_days`). A run document that outlived its recordings would be study data about a participant whose data was deleted.
6. **Tests.**
   - The S3 copy equals the EFS file after create, advance and withdraw.
   - A failing upload does not fail `/advance` or `/withdraw`.
   - The startup sweep uploads a missing run.
   - The HQ transcript and the fallback webcam are in the archive list.
   - The IAM statement includes `runs/*`, and the lifecycle includes `runs/`.

This is a release of step 1, so it goes through step 1's own runbook: a pin PR, `tools/deploy.sh`, and an apply between collection sessions.

## What the rater app already does with it

Nothing needs to change here:
- `server/source.py` already reads `runs/<run_id>.json`, answering `None` while the object does not exist.
- The task role already has `GetObject` on `runs/*` and `ListBucket` on the `runs/` prefix.

Once runs are exported, the next change in this repository is to refuse, or flag, entries whose run carries `withdrawn`.
