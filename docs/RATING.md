# Rating: how step 2 works

**Decided 2026-10-22:** step 2's raters are trained team members, not participants. **Connected 2026-11-05:** the rating screen is the UI prototype, fed by the app. Each rater gets a batch of four recordings the app picks, watches or listens to one at a time, and scores the participant on that scenario's construct items, with an example for every score of 2 to 5. What is still open is listed at the end.

## The rater's path

1. A researcher creates the rater and gets a one-time link (below).
2. The rater opens the link once. The app sets a signed, HttpOnly session cookie (12 hours) and redirects to `/rate`, so the token leaves the address bar.
3. `/rate` is the rating screen ([`static/rater/rate.html`](../static/rater/rate.html)). On a first visit it asks the app for a **batch** (`POST /api/batch`): up to four recordings, the four stages in the side menu, opened in order.
4. Each stage: the recording, the transcript (click a time to jump there), the scenario's five or six questions one at a time, 1 Never to 5 Consistently. A score of 2 to 5 needs an example: a participant line from the transcript, or a clip of the recording. Answers are saved to the app as a draft as the rater goes; a reload, or another browser, resumes.
5. A review page, then Submit. A recording is submitted once, and only after the ones before it. After the last, the rater gets a **completion code** (`RFR-XXXX-XXXX`), and can ask for another batch.

A rater who recognises a participant is told to stop and contact the study team.

## Which recordings a rater gets

The app picks them (`allocate_batch` in [`server/rating.py`](../server/rating.py)):

- **Every recording is rated twice** (`RATINGS_PER_SESSION`), by two different raters.
- **A rater never gets two recordings of the same participant**, in one batch or across batches. So the four stages are four different people, and a participant's four recordings, rated twice each, go to eight different raters.
- Only encounters with a participant key, a scenario id this app knows (`S1A` to `S4B`), a cohort in `RATE_COHORTS` (`study` unless set; on the deployed site it is `rate_cohorts` in `infra/terraform/terraform.tfvars`) and a recording.
- The least-rated recordings first, at random among equals.
- When fewer than four are left that this rater may have, the batch is shorter; when none are, the screen says there is nothing to rate.

A recording's ratings are counted by slots, `slots/<session_id>/1.json` and `2.json`, each written create-only, so two raters asking at the same moment cannot both take the last one. A slot is taken when the batch is given out, not when it is submitted: a rater who never finishes holds their slots (see the open list).

## What a rater can and cannot see

Blind by construction, checked by `tests/test_rating.py`, which greps every rater-facing response for step 1's shapes.

| Shown | Never shown |
|---|---|
| Rating code `RC-` + 10 hex digits, stage in the batch | Session id, participant key, run id, cohort |
| Transcript as "Participant" and "Character" (or "Character 1, 2, …" in a group scene) | Character names as labels, scenario id or title, step 1's stage directions and item tags |
| The recording, streamed by this app at `/rate/<code>/media` | The S3 object key (it carries the session id) |
| The scenario's construct's items, in an order fixed per rater and code | |

Two limits the labels cannot remove. A name spoken in the conversation ("Hey Riley") is in the audio and in the transcript text. And because each recording asks only its construct's items, as the prototype was designed, the rater can tell which competency a scenario targets.

The rating code is derived from the rater id and the session id, so the same encounter has a different code for every rater and nobody can line up two raters' batches from the codes alone.

## The instrument

[`server/items.py`](../server/items.py): the 21 items, five or six per construct, keyed exactly as step 1's `scenarios/v3/*.yaml` `esci_items` and as the analysis database's `rf.esci_item`, so a rating row can be loaded there without a mapping. A scenario id's number is its construct (S1 conflict management, S2 influence, S3 inspirational leadership, S4 teamwork). Scale 1 Never, 2 Rarely, 3 Sometimes, 4 Often, 5 Consistently. Reverse-keyed items (`*_r`) keep their marker in the data and lose it on screen. `tests/test_items.py` compares the table with step 1's YAML when it is checked out beside this repository.

## The recording

The webcam recording (`webcam.webm`, the camera with the whole conversation's audio) when the browser uploaded one. Else step 1's mix of the conversation (`conversation_audio.wav`) where the archive has it, else the participant's own channel (`user_audio.wav`), where the characters are only in the transcript. An encounter with none of these is not given out. The transcript's times are step 1's session clock; the webcam recording starts when the camera does, which can be a second or two later. The app proxies the file from step 1's bucket with `Range` support, so the browser seeks normally and the bucket is never addressed from the browser.

## Where it is stored

This app's bucket, under prefixes `server/store.py` and `infra/terraform/storage_secrets.tf` both list (`tests/test_terraform.py` keeps them equal). Everything is JSON; nothing is deleted by the app.

| Prefix | One object per | Holds |
|---|---|---|
| `raters/` | rater | id, display name, SHA-256 of the link token, active |
| `assignments/<rater>/` | rating code | the step 1 references (participant key, session id, run id, cohort), batch, stage, scenario, construct, completion code, status, done_at |
| `slots/<session id>/` | rating given out | which rater and code took the first or second rating of a recording |
| `drafts/<rater>/` | rating code | autosaved scores, examples and comments; overwritten on every save |
| `ratings/<rater>/` | rating code | the submission: scores, examples (a line's index, time and words, or a clip's start and end in seconds), comments, item order, which recording and transcript were used, plus the step 1 references |

A rating is a record in `store.py`'s sense: it points back at step 1, so `/api/ratings?format=csv` joins without a lookup, and the analysis database can be loaded from it (plan: [relational-fluency-rater#1](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency-rater/pull/1)).

## Researcher runbook

Every call takes the researcher key in the `X-Researcher-Key` header. Locally the key is whatever `.env` says (empty allows all on a loopback bind). `?key=` is still accepted, but a URL lands in shell history and request logs; the access log drops query values (`server/logsafe.py`) and a header never gets there at all.

```bash
H=https://rater.ai-ready-workforce.ai.cornell.edu   # locally: http://127.0.0.1:8766
KEY=$(aws secretsmanager get-secret-value --secret-id relational-fluency-rater/researcher-key --query SecretString --output text)
R=(-H "X-Researcher-Key: $KEY")

# 1. Create a rater. The token is shown ONCE; send the link to the rater privately.
curl -s "${R[@]}" -X POST "$H/api/raters" -H 'Content-Type: application/json' \
     -d '{"rater_id": "R-07", "display_name": "Seven"}'

# 2. Nothing to assign: the rater's screen asks for its own batch. To see what is
#    waiting, and how far each recording is from its two ratings:
curl -s "${R[@]}" "$H/api/coverage"

# 3. Progress, and the ratings so far.
curl -s "${R[@]}" "$H/api/raters"
curl -s "${R[@]}" "$H/api/assignments?rater_id=R-07"   # with each batch's completion code
curl -s "${R[@]}" "$H/api/ratings"                   # full records
curl -s "${R[@]}" "$H/api/ratings?format=csv"        # long format, one row per item
```

`/api/step1/participants/PARTICIPANT_KEY/encounters`, with the same header, still shows what step 1 holds for one person.

## Local development

Step 1's local data is usually a team test run or two, too few people to fill a batch. [`tools/make_demo_step1.py`](../tools/make_demo_step1.py) writes made-up participants (the prototype's four example conversations, a quiet WAV with a tone at each participant line, or `--video` with a webm of your own) in step 1's layout, in cohort `internal`:

```bash
python -m tools.make_demo_step1 data/demo-step1            # six made-up participants, 24 encounters
SOURCE_DIR=data/demo-step1 RATE_COHORTS=internal python -m server.app
curl -s -X POST 'http://127.0.0.1:8766/api/raters' -H 'Content-Type: application/json' -d '{"rater_id":"R-01"}'
# open the `link` it prints, with the host replaced by http://127.0.0.1:8766
```

Against step 1's own sessions instead: `SOURCE_DIR=../relational-fluency/data RATE_COHORTS=study,internal`. Records go under `data/rater-records/`; delete `assignments/`, `slots/`, `drafts/` and `ratings/` there to start over. [`TESTING-LOCALLY.md`](TESTING-LOCALLY.md) has the rest.

## Still open

1. **How many raters.** Two ratings per recording, and no rater twice on one participant, means at least eight raters. Batches per rater are unlimited.
2. **Abandoned batches.** A slot is held from the moment a batch is given out. A rater who stops keeps their recordings from anyone else; there is no expiry or hand-back yet (by hand: delete their unrated assignment and its slot).
3. **The completion code.** The screen still says "Paste it into CloudResearch Connect to finish", as the prototype did; team raters signing in with a link have nowhere to paste it. Keep, reword or drop.
4. **Item permission.** Showing the ESCI items to raters is a licensing question for the study team.
5. **IRB.** Whether the protocol covers raters streaming webcam video in the browser.
6. **Sign-in.** A per-rater link today; Cornell sign-in would replace `rater_for_token`.
7. **Blinding detail.** The participant's briefing is not shown (it is not in the archive this app reads); character names are withheld as labels. The construct is not hidden (above).
8. **Withdrawn participants.** Not visible to this app until step 1 exports runs ([`step1-runs-export.md`](step1-runs-export.md)); until then a withdrawn participant's recordings can be given out.
9. **The Qualtrics entry (`/start`).** Built when participants were going to be the raters. It still works and still writes `entries/`; it is not the way into step 2 any more and can be removed once this decision is confirmed in writing.
