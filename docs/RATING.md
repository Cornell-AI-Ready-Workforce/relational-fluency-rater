# Rating: how step 2 works

**Decided 2026-10-22:** step 2's raters are trained team members, not participants. They work from a queue, watch or listen to one step 1 conversation at a time, and score the participant on all 21 ESCI items. The design follows the "Relational Fluency Annotation" prototype; what it leaves open is listed at the end.

## The rater's path

1. A researcher creates the rater and gets a one-time link (below).
2. The rater opens the link once. The app sets a signed, HttpOnly session cookie (12 hours) and redirects to `/rate`, so the token leaves the address bar.
3. `/rate` is the queue: one row per **rating code**, with its position and whether it is rated.
4. `/rate/<code>` is the rating screen: the recording, the transcript (click a row to jump there), the 21 items, two optional comment boxes, Submit. Answers are saved as a draft as the rater goes; a reload resumes.
5. Submit writes the rating once. A second submit, or a draft after it, is refused.

A rater who recognises a participant is told to stop and contact the study team.

## What a rater can and cannot see

Blind by construction, checked by `tests/test_rating.py`, which greps every rater-facing response for step 1's shapes.

| Shown | Never shown |
|---|---|
| Rating code `RC-` + 10 hex digits, position in the queue | Session id, participant key, run id, cohort |
| Transcript as "Participant" and "Character" (or "Character 1, 2, …" in a group scene) | Character names as labels, scenario id or title, the targeted competency |
| The recording, streamed by this app at `/rate/<code>/media` | The S3 object key (it carries the session id) |
| All 21 items, in an order fixed per rater and code and mixed across constructs | Which five items belong to the scenario's construct |

One limit the labels cannot remove: a name spoken in the conversation ("Hey Riley") is in the audio and in the transcript text. Raters are told nothing about which names go with which scenario, and the item set is the same for every encounter, so a name alone does not say what is being measured.

The rating code is derived from the rater id and the session id, so the same encounter has a different code for every rater and nobody can line up two raters' queues from the codes alone.

## The instrument

[`server/items.py`](../server/items.py): the 21 items, keyed exactly as step 1's `scenarios/v3/*.yaml` `esci_items` and as the analysis database's `rf.esci_item`, so a rating row can be loaded there without a mapping. Scale 1 Never, 2 Rarely, 3 Sometimes, 4 Often, 5 Consistently, and `N/A` for an item the conversation gave no chance to show. Reverse-keyed items (`*_r`) keep their marker in the data and lose it on screen. `tests/test_items.py` compares the table with step 1's YAML when it is checked out beside this repository.

## The recording

The webcam recording (`webcam.webm`) when the browser uploaded one, else the participant's audio channel (`user_audio.wav`). The characters' audio is per character in separate files and is not mixed in; the transcript carries their lines. With neither, the screen says so and the rater scores from the transcript. The app proxies the file from step 1's bucket with `Range` support, so the browser seeks normally and the bucket is never addressed from the browser.

## Where it is stored

This app's bucket, under prefixes `server/store.py` and `infra/terraform/storage_secrets.tf` both list (`tests/test_terraform.py` keeps them equal). Everything is JSON; nothing is deleted by the app.

| Prefix | One object per | Holds |
|---|---|---|
| `raters/` | rater | id, display name, SHA-256 of the link token, active |
| `assignments/<rater>/` | rating code | the step 1 references (participant key, session id, run id, cohort), position, status, done_at |
| `drafts/<rater>/` | rating code | autosaved answers; overwritten on every save |
| `ratings/<rater>/` | rating code | the submission: scores, comments, item order, which recording and transcript were used, plus the step 1 references |

A rating is a record in `store.py`'s sense: it points back at step 1, so `/api/ratings?format=csv` joins without a lookup, and the analysis database can be loaded from it (plan: [relational-fluency-rater#1](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency-rater/pull/1)).

## Researcher runbook

Every call takes `key=RESEARCHER_KEY`. Locally the key is whatever `.env` says (empty allows all on a loopback bind).

```bash
H=https://rater.ai-ready-workforce.ai.cornell.edu   # locally: http://127.0.0.1:8766

# 1. Create a rater. The token is shown ONCE; send the link to the rater privately.
curl -s -X POST "$H/api/raters?key=$KEY" -H 'Content-Type: application/json' \
     -d '{"rater_id": "R-07", "display_name": "Seven"}'

# 2. Which encounters a participant has (session ids), then assign them.
curl -s "$H/api/step1/participants/PARTICIPANT_KEY/encounters?key=$KEY"
curl -s -X POST "$H/api/assignments?key=$KEY" -H 'Content-Type: application/json' \
     -d '{"rater_id": "R-07", "session_ids": ["s_1727000000_bbbbbb", "s_1727000001_aaaaaa"]}'
#    -> created codes, already_assigned codes, and refused session ids with the reason
#       (not in step 1, or no participant key: a test or recovered encounter)

# 3. Progress, and the ratings so far.
curl -s "$H/api/raters?key=$KEY"
curl -s "$H/api/assignments?key=$KEY&rater_id=R-07"
curl -s "$H/api/ratings?key=$KEY"                   # full records
curl -s "$H/api/ratings?key=$KEY&format=csv"        # long format, one row per item
```

Spreading one participant's four encounters across different raters is the researcher's job when assigning; the app does not allocate.

## Local development

```bash
cp .env.example .env             # SOURCE_DIR=../relational-fluency/data to read step 1's local sessions
python -m server.app
curl -s -X POST 'http://127.0.0.1:8766/api/raters' -H 'Content-Type: application/json' -d '{"rater_id":"R-01"}'
# open the `link` it prints, with the host replaced by http://127.0.0.1:8766
```

Records go under `data/rater-records/`. [`TESTING-LOCALLY.md`](TESTING-LOCALLY.md) has the rest.

## Still open (from the prototype's list)

1. **Who the raters are and how many per encounter.** The app takes any number; allocation is by hand.
2. **Item permission.** Showing the ESCI items to raters is a licensing question for the study team.
3. **IRB.** Whether the protocol covers raters streaming webcam video in the browser.
4. **Sign-in.** A per-rater link today; Cornell sign-in would replace `rater_for_token`.
5. **Blinding detail.** The participant's briefing is not shown (it is not in the archive this app reads); character names are withheld. Both can change in one place (`_transcript_rows`).
6. **Evidence.** Whole-encounter scores only. Cited transcript rows or marked moments would add a field to the rating and a control to the screen.
7. **Withdrawn participants.** Not visible to this app until step 1 exports runs ([`step1-runs-export.md`](step1-runs-export.md)); until then, check before assigning.
8. **The Qualtrics entry (`/start`).** Built when participants were going to be the raters. It still works and still writes `entries/`; it is not the way into step 2 any more and can be removed once this decision is confirmed in writing.
