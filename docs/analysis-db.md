# Plan: step 2 ratings in the analysis database

**Status (2026-10-22): a plan, not implemented.** Decision taken 2026-10-22: step 2 ratings go into the existing `rf` schema on step 1's RDS instance (`rf.rater`, `rf.rating_assignment`, `rf.rating`), not into a schema of this app's own. This page says what has to be true, in which repository, for this app to write there.

The schema lives in step 1: [`docs/db-schema.sql`](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency/blob/main/docs/db-schema.sql), explained in [`docs/db-schema.md`](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency/blob/main/docs/db-schema.md), roles in [`docs/db-roles.sql`](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency/blob/main/docs/db-roles.sql), loader [`tools/load_analysis_db.py`](https://github.com/Cornell-AI-Ready-Workforce/relational-fluency/blob/main/tools/load_analysis_db.py). Instance `relational-fluency-analysis`, database `rf`, Postgres 16, `db.t4g.micro`, public endpoint, TLS forced, administered by Tanvi (`tanvi-cli`).

## What is true today

| | State on 2026-10-22 |
|---|---|
| RDS instance | running since 2026-09-23. One connection on 2026-09-24, none since. 18.3 GB of 20 GB free |
| Schema applied? | **unverified**. The single connection could have been the schema load or just a look. Check first (below) |
| Loaded with encounters? | almost certainly not: nothing has connected since 2026-09-24 |
| Who can reach it | security group admits `128.84.125.179/32` and step 1's task security group only |
| This app's stack | never applied: no bucket, no VPC `10.41.0.0/16`, no `rater/terraform.tfstate` |
| This app's pages | `static/*.html`, filled and served by `server/app.py`; forms post back to it (see "How the pages talk to the server") |

## How the pages talk to the server

There is no separate front end. The "front end" is three HTML files in [`static/`](../static/) and the "back end" is one FastAPI process ([`server/app.py`](../server/app.py)) that serves them and everything else on one origin:

1. The participant arrives on `GET /start?pid=…&qid=…` from Qualtrics. The handler reads `static/start.html` as text, replaces `{{message}}` and `{{hidden}}` ([`_page()`](../server/app.py)), and returns it. The page's `<form method="post" action="/start">` posts straight back.
2. `POST /start` writes the entry record (`entries/<entry_id>.json`) and answers `303 /visit?v=<entry_id>`.
3. `GET /visit` fills `static/visit.html` and returns it. That page is the step 2 task placeholder.
4. Researcher routes (`/api/entries`, `/api/step1/...`) return JSON and are gated by the researcher key.

Two rules keep it this way: the Content-Security-Policy is `default-src 'self'`, so a page can only fetch or post to this same server, never to RDS, S3 or any other host; and the pages use no framework and no build step. A rating page follows the same pattern: `GET /visit` renders the transcript into the HTML (or the page calls `GET /api/visit/<entry_id>/encounters` on the same origin), and the form or a `fetch()` posts the scores to `POST /api/ratings`. **The database is only ever touched by the server process.** Nothing in the browser knows the database exists.

## The target tables, and what they assume

```sql
rater             (rater_id text PK, display_name, trained_at date, active bool)
rating_assignment (assignment_id bigserial PK, encounter_id → encounter, rater_id → rater,
                   assigned_at, due_at, status assigned|in_progress|done|withdrawn,
                   UNIQUE (encounter_id, rater_id))
rating            (rating_id bigserial PK, assignment_id → rating_assignment,
                   construct, item_key → esci_item, score 1..5, evidence text,
                   evidence_turn_ids bigint[], rated_at,
                   UNIQUE (assignment_id, construct, item_key))
```

Each foreign key is a precondition on the database, not on this app:

- **`rating_assignment.encounter_id → encounter`.** The encounter must already be loaded. Encounter ids are step 1's session ids (`s_<epoch>_<hex>`), the same string this app sees in `manifest.json`, so no mapping is needed. But the loader is run by hand from a laptop; a participant whose step 1 encounters closed after the last load cannot be rated until someone loads again. Either the loader becomes a scheduled task (open question 2 in `db-schema.md`) or this app checks for the row and shows "not ready yet" rather than a 500.
- **`rating.(construct, item_key) → esci_item`.** The ESCI item keys come from `scenarios/v3/*.yaml` through `load_scenarios()`. The rating form's item keys must be those exact strings; take them from the database (`SELECT item_key, label FROM esci_item WHERE construct = …`), never from a copy in this repo.
- **`rating.evidence_turn_ids`** points at `turn.turn_id`, a `bigserial`. **Those ids are not stable:** `load_encounter()` deletes every child row of an encounter and re-inserts them on each refresh, so a reload renumbers every turn. Store `(encounter_id, seq)` in the rating instead, which is what the record carries and what survives a reload; or change the loader to upsert turns on `(encounter_id, seq)`. This needs a decision in step 1 before the first real rating.
- **The loader deletes the encounter row** (`DELETE FROM encounter WHERE encounter_id = …`) before re-inserting it. `rating_assignment.encounter_id` has no `ON DELETE` clause, so once a single assignment exists, the next refresh of that encounter fails with a foreign-key violation. The loader must change to update the encounter row in place (or the constraint must defer). This is the one change that is not optional.
- **`rater_id`** is whoever rates. In the Phase 2 design that was a trained human; in step 2 it is probably the participant (rating their own or another's conversation) or a team member. Use this app's `entry_id` (`e_<24 hex>`) as the `rater_id` for participants, with `display_name` NULL, and a short handle for team members. Decide this with the task design; the column is free text, so nothing blocks either choice.

A benefit of the connection: once the loader is run with `--runs`, this app can read `rf.withdrawal` and `rf.run` and refuse entries for withdrawn participants, which it cannot do from S3 today ([`step1-runs-export.md`](step1-runs-export.md)).

## Network: how a task reaches the instance

The instance has a public address but its security group is an allowlist. Fargate tasks in this stack have **dynamic** public IPs (no NAT), so they cannot be allowlisted by address. Two ways:

| | VPC peering `10.41/16 ↔ 10.40/16` (recommended) | NAT gateway with an Elastic IP |
|---|---|---|
| Fixed cost | none; intra-region transfer ~$0.01/GB each way | ~$33/month plus the EIP |
| Path | private, never leaves AWS | out the public endpoint |
| Step 1 change | security-group rule from this app's task SG, routes, peering options | one CIDR in `analysis_db_allowed_cidrs` |
| This stack | peering connection, routes, egress rule | NAT, EIP, routes |

`network.tf` already chose `10.41/16` "so the two could be peered later". With peering:

**In this stack (`infra/terraform/`):**
- `data "aws_vpc" "step1"` by tag `Name = relational-fluency` (data source only; add it to the list in `tests/test_terraform.py::test_step1_resources_are_data_sources_only`).
- `aws_vpc_peering_connection` with `peer_vpc_id = data.aws_vpc.step1.id`, `auto_accept = true` (same account and region), and `aws_vpc_peering_connection_options` with `allow_remote_vpc_dns_resolution = true` on both sides. Without the DNS option the RDS hostname resolves to its **public** IP from this VPC and the traffic goes out the internet, where the security group rule by SG id does not match.
- `aws_route` on each of `module.vpc.public_route_table_ids`: `10.40.0.0/16 → pcx`.
- `aws_security_group.app`: a second egress rule, `5432/tcp → 10.40.0.0/16`. Today egress is 443 only, on purpose; the comment there should say why 5432 is added.
- An output `app_security_group_id` for step 1 to read.

**In step 1 (`infra/terraform/analysis_db.tf`, `network.tf`):** the security group uses inline `ingress` blocks, so the new rule must be added there, not from this stack (a standalone rule resource fights inline blocks on every plan).
- A variable `analysis_db_peer_security_group_ids` (default `[]`) and an `ingress` block on `aws_security_group.analysis_db` with `security_groups = var.analysis_db_peer_security_group_ids`, description "Postgres from the rater app tasks (peered VPC)".
- `data "aws_vpc_peering_connection"` filtered on this stack's VPC and `aws_route` entries on step 1's **public** route tables (the RDS subnets are the public ones): `10.41.0.0/16 → pcx`.
- Both are settings releases through `tools/deploy.sh`; the plan should touch only the security group and route tables.

Order: this stack's base apply first (it is the one that creates the peering), then step 1's release that references it.

## Credentials and TLS

- **A role of its own**, `rf_rater`, created by the administrator in `db-roles.sql`: `LOGIN NOSUPERUSER`, `SELECT` on `encounter`, `turn`, `scenario`, `esci_item`, `construct`, `run`, `withdrawal`, `transcript_version`; `SELECT, INSERT, UPDATE` on `rater`, `rating_assignment`, `rating`; `USAGE, SELECT` on the sequences; **nothing** on `participant_identity`. This app already knows the participant key from Qualtrics and matches encounters by session id, so it never needs the identity table. Not `rf_admin` (RDS rotates that password weekly), not `rf_analyst` (people's role), not `rf_loader` (writes everything).
- **The password is set with `\password rf_rater`** by the administrator and stored once, as a full DSN, in a new Secrets Manager secret `relational-fluency-rater/db-url` (`storage_secrets.tf`, beside `researcher-key`). The task-execution role's `read_secrets` policy gains its ARN; the task definition gains `{ name = "DATABASE_URL", valueFrom = … }`. `tests/test_terraform.py::test_task_definition_matches_the_image` asserts the exact `secrets` lines, so it changes too.
- **TLS `verify-full`**, not `require`. The instance forces TLS, but `require` does not check the certificate. Bake the RDS bundle into the image (`COPY certs/global-bundle.pem /app/certs/`) and put `sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem` in the DSN. `tests/test_docker.py` requires the Dockerfile `COPY` set to equal the `.dockerignore` allowlist, so both change together.
- **Connection budget.** `db.t4g.micro` allows about 110 connections. A pool of min 1, max 4 per task is plenty; one task runs.

## Changes in this repository

1. **Driver.** `psycopg[binary]` and `psycopg-pool`, exact pins. `tests/test_parity.py::test_shared_requirement_pins_match_step1` fails on any package step 1 does not pin ("pin it there first"), and step 1 installs psycopg by hand in its docs. So the pin lands in step 1's `requirements.txt` first, then here at the same version.
2. **`server/db.py`.** The pool, built from `DATABASE_URL` at startup; `SET search_path TO rf, public` per connection; a `ping()` for `/health`. With `DATABASE_URL` unset the pool is `None`, rating routes answer 503 "no database", and everything that exists today keeps working: the database is a dependency of the rating task, not of entry.
3. **`server/ratings.py`.** `encounters_ready(session_ids)` (which of the participant's step 1 encounters have an `encounter` row), `items_for(construct)`, `turns(encounter_id)`, and `submit(entry_id, encounter_id, scores)`: one transaction that upserts the `rater` row, inserts or finds the `rating_assignment`, inserts the `rating` rows, sets the assignment `done`. Scores are validated against `esci_item` before the transaction, so a bad item key is a 400, not a rolled-back 500.
4. **Write the record to S3 too.** Keep the rule that S3 is the record and the database a copy that can be rebuilt (`db-schema.md`, "Scope"). Each submission is written as `ratings/<entry_id>/<encounter_id>.json` through `RecordStore` **before** the database transaction, so a database failure leaves a record a backfill can replay. That adds `ratings/` to `WRITE_PREFIXES` in `server/store.py` and to `local.rater_write_prefixes` in `storage_secrets.tf`; `tests/test_terraform.py::test_write_prefixes_match_the_app` holds the two equal.
5. **Routes and page.** `GET /api/visit/{entry_id}/encounters` (the participant's encounters with turns, from the database) and `POST /api/ratings`, both bound to the entry id in the URL rather than a researcher key, since the participant is the caller. `visit.html` gains the rating form. Researcher side: `GET /api/ratings?key=…`.
6. **`/health`** gains `"db": "ok" | "down" | "unconfigured"`; `tools/deploy.sh` already reads `/health` before a plan.
7. **Tests.** Everything in `server/ratings.py` is testable with a fake cursor, as step 1's loader is. One integration test behind `RATER_TEST_DSN` runs the real SQL against a local `postgres:16-alpine` loaded with `db-schema.sql`, the way `db-schema.md` describes.

## Changes in step 1 (a PR there, not from here)

1. `tools/load_analysis_db.py`: update the `encounter` row in place and upsert `turn` on `(encounter_id, seq)`, so a refresh neither fails on an existing `rating_assignment` nor renumbers turns. Replace-and-reinsert can stay for the rows nothing references (`event`, `director_route`, `trigger_firing`, `stage_direction`, `media`, `transcript_version`).
2. `docs/db-roles.sql`: the `rf_rater` role and its grants.
3. `infra/terraform/analysis_db.tf` and `network.tf`: the ingress rule, the peering routes, the variable.
4. `requirements.txt`: pin `psycopg[binary]` and `psycopg-pool`.
5. Optional but worth it once two writers exist: a `schema_version` table and a `docs/migrations/` directory. Today the schema is "draft 1" and `ensure_pipeline_columns()` does ad hoc `ALTER TABLE`s; with this app depending on column names, a change in step 1 is a coordinated release.

## Who does what, in order

| # | Who | What | Where |
|---|---|---|---|
| 1 | Tanvi | Confirm the state of the instance: `psql … -c '\dn'` shows schema `rf`? `\dt rf.*` shows tables? `SELECT count(*) FROM rf.encounter`? From the allowlisted address only | laptop |
| 2 | Tanvi | If empty: apply `db-schema.sql`, `db-roles.sql`, run the loader with `--runs` and `--s3-media` | step 1 runbook |
| 3 | step 1 PR | loader fixes (in-place encounter, stable turns), `rf_rater` grants, psycopg pins | relational-fluency |
| 4 | admin | Apply this stack's base (already required; creates the peering) | this repo, `DEPLOY-AWS.md` |
| 5 | step 1 PR + release | `analysis_db_peer_security_group_ids = [<this app's SG>]`, routes | relational-fluency |
| 6 | Tanvi | `\password rf_rater`; `aws secretsmanager put-secret-value --secret-id relational-fluency-rater/db-url` | laptop |
| 7 | this repo PR | items 1–7 above | relational-fluency-rater |
| 8 | anyone | Decide the loader cadence. Until it is scheduled, every wave needs a load before participants can rate | step 1 |

Steps 1 and 2 can start now and are independent of everything else. Step 4 is the first real apply of this stack and needs an administrator for the IAM roles regardless of the database.

## Local development

```bash
# step 1's local database, same schema (from step 1's docs/db-schema.md)
docker run -d --name rf-analysis-db -p 5433:5432 -e POSTGRES_PASSWORD=rf -e POSTGRES_DB=rf \
    -v rf-analysis-pgdata:/var/lib/postgresql/data postgres:16-alpine
docker exec -i rf-analysis-db psql -q -U postgres -d rf -f - < ../relational-fluency/docs/db-schema.sql
# load step 1's local sessions into it (from the step 1 checkout)
python tools/load_analysis_db.py --dsn postgresql://postgres:rf@localhost:5433/rf \
    --archive data/sessions --scenarios scenarios/v3
```

Then in this repo's `.env`: `DATABASE_URL=postgresql://postgres:rf@localhost:5433/rf`. The production DSN never goes in `.env`; it exists only in Secrets Manager.

## Open decisions

1. **Who is the rater** (participant, peer, team member) and therefore what `rater_id` is. Follows the step 2 task design.
2. **Turn references:** `(encounter_id, seq)` in this app, or stable `turn_id` from a fixed loader. Recommended: fix the loader anyway (it must change for the encounter delete), and store `seq` here too.
3. **Loader cadence:** by hand per wave, or a scheduled ECS task in step 1 (its security group is already admitted).
4. **Whether `verify-full` is worth the baked-in bundle** or `require` is enough for a private peered path. Recommended: `verify-full`; the bundle is 200 KB.
