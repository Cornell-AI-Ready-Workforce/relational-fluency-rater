# Testing locally

Install and start the app as in the [README](../README.md#run-it-locally), then:

```bash
curl -s http://127.0.0.1:8766/health
```

## The three things nothing else tells you

1. **Where step 1 data comes from.**
   - `SOURCE_DIR=../relational-fluency/data` reads step 1's *local* sessions (its `data/sessions/<id>/` layout) from a step 1 checkout beside this one. Nothing is written there.
   - `SOURCE_BUCKET=relational-fluency-study-data` reads the real archive with your own AWS credentials. Your identity needs `s3:GetObject`, `s3:ListBucket` and `kms:Decrypt`. Those reads are real access to study data, so only do this if you are on the protocol.
   - With neither set, step 1 has no data and every participant has no encounters.
2. **Where records go.** With `RATER_BUCKET` unset, records go to `data/rater-records/entries/` in this checkout, which is gitignored. Setting `RATER_BUCKET` writes to the production bucket; there is no staging bucket. The app refuses to write to step 1's bucket whatever you set.
3. **Port 8766.** Step 1 uses 8765, so both can run at once.

## An entry, end to end

```bash
# macOS / Linux
SOURCE_DIR=../relational-fluency/data python -m server.app
```

```powershell
# Windows PowerShell
$env:SOURCE_DIR = "..\relational-fluency\data"; python -m server.app
```

Then, in a browser (not curl, which the link-preview rule treats as a fetcher):

- <http://127.0.0.1:8766/start?pid=selftest1> redirects to `/visit?v=e_…` and writes one record. `selftest1` must be a participant key that exists in your step 1 data for the record to name any encounters.
- <http://127.0.0.1:8766/api/entries> shows it, with `step1.session_ids` and `step1.run_id`. No key is needed locally while `RESEARCHER_KEY` is empty; a public bind refuses to start that way.
- <http://127.0.0.1:8766/start?pid=${e://Field/participantId}> shows the Continue page. Pressing Continue files you as `unattributed`.

To find a key that exists in your step 1 data:

```bash
python -c "import json,glob; print(sorted({json.load(open(f)).get('participant_key') for f in glob.glob('../relational-fluency/data/sessions/*/manifest.json')} - {None}))"
```

## Tests

```bash
pip install pytest httpx2
python -m pytest tests -q -rs
```

With step 1 checked out at `../relational-fluency` (or at `RF_STEP1_REPO`), the parity tests compare `server/conventions.py`, `server/identity.py`, `server/build_info.py`, `tools/pinned_image.py`, the shared requirement pins, the copied assets and the theme's tokens with step 1's source. Without it, those comparisons skip and say why, and the golden vectors still run. `tests/test_deploy_guard.py` runs the real `tools/deploy.sh` against a throwaway git repository with `aws`, `curl` and `tofu` stubbed; it needs bash and git.

## In Docker

```bash
docker build --platform linux/amd64 --build-arg BUILD_SHA=$(git rev-parse --short HEAD) -t rater:local .
docker run --rm --read-only -p 8766:8080 -e RESEARCHER_KEY=dev -e ALLOWED_HOSTS=localhost,127.0.0.1 rater:local
```

`--read-only` matches production (`readonlyRootFilesystem` in `ecs.tf`). With no `RATER_BUCKET` the container has nowhere to write records, so use this to check the image starts and serves `/health`, not to test entries.
