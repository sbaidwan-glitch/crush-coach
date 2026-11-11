# Crush Coach

A task tracker with an AI coach built into every task, for the MuddSub (Harvey Mudd RoboSub) mechanical team. Members claim a task on the robot "Crush", the coach answers questions from the team's own Google Drive and walks them through each stage, leads review the work in the app, and the tracker updates itself as people work.

Owner: Samara Baidwan (mechanical co-lead). Course: E190BO AI-Amplified Prototyping, Fall 2026 final project.

## Stack (course default; don't add frameworks without asking)
- Python 3.13, FastAPI, SQLModel, Alembic migrations, Postgres (psycopg 3)
- Frontend: plain HTML, CSS, JavaScript + htmx, served by FastAPI. No build step, no React (unless Samara asks).
- LLM: called only from the server, only on a user's action, never on page load. Key: `GLASSROOM_API_KEY` (env only).
  - Glassroom is an OpenAI-compatible LiteLLM proxy. Use the pinned `openai` library with `base_url="https://litellm.glassroomai.com/v1"` and `api_key` from `GLASSROOM_API_KEY`.
  - Model comes from `COACH_MODEL` (default `claude-haiku-4-5`).
  - All model code lives in `src/llm.py`; no other module imports `openai`.
  - Coach settings (env): `COACH_DAILY_LIMIT` (messages per member per day, default 20), `COACH_TIMEZONE` (day boundary, default America/Los_Angeles), `COACH_TIMEOUT_SECONDS` (default 30).
- Google: Drive API v3 (read) + Docs API v1 (append-only write-back), via a service account.
- Deploy: Docker on Railway, with PR environments. Pinned versions in `requirements.txt`; re-pin with `pip freeze` if you add a package.

## Commands
- Use the workspace `.venv` for everything: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`; never the system Python. Create it with `python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- `docker compose up -d`: local Postgres (matches `DATABASE_URL` in `.env.example`)
- `make dev`: run the API on :8000 from `src/`
- `make test`: run tests from `src/`
- `make eval`: score the coach on `evals/coach_questions.yaml` with the real model and Drive (costs money; `ONLY=<id>` or `KIND=<kind>` to run less). See "Measuring the coach"
- `make screenshots`: screenshot `/`, `/tasks` and `/tasks/M04` at 375px and 1280px into `.context/screenshots/` (gitignored), against a throwaway SQLite database with the sample tasks and a fake signed-in session. One-time setup: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/playwright install chromium`
- `.venv/bin/ruff check src`: lint
- `cd src && ../.venv/bin/python scripts/drive_search.py "latch"`: search the team shared drive (metadata only); `--type cad|sheets|docs|pdf` filters by file type (the query may be empty with `--type`)
- `cd src && ../.venv/bin/python scripts/drive_list.py "Mechanical/CAD"`: list a Drive folder by path, id or link (names, kinds, modified dates)
- `cd src && ../.venv/bin/python scripts/ask_coach.py "question"`: ask the coach one real question (real model and Drive; no database); prints progress steps; `--follow-up "second question"` continues the chat; `--code/--title/--subsystem/--type/--stage` set the task
- `cd src && ../.venv/bin/python -m scripts.sync_drive --full` / `--incremental`: sync the team Drive into the local index (Postgres; the Railway cron job runs `--incremental` every 30 min). `--full --force` re-reads unchanged files. Needs the Google credentials and `DATABASE_URL`; exits 1 if the whole sync fails, 0 if only some files' text couldn't be read
- `cd src && ../.venv/bin/python scripts/index_search.py "lid latch"`: search the local index by hand (what the coach will see in the next PR); `--limit`, `--full-text`
- To run the Postgres-only search tests: `TEST_POSTGRES_URL=postgresql://e190bo:localdev@localhost:5432/app make test` (empties the Drive index tables, so use a throwaway database)
- Migrations (run from `src/`; the app also runs `upgrade head` on startup, and a failure there is logged but doesn't stop the app):
  - `cd src && ../.venv/bin/alembic upgrade head`: apply all migrations
  - `cd src && ../.venv/bin/alembic revision --autogenerate -m "describe change"`: create a migration from model changes (needs `DATABASE_URL` pointing at a database already at head; review the generated file before committing)
  - `cd src && ../.venv/bin/alembic downgrade -1`: undo the last migration
  - `cd src && ../.venv/bin/alembic stamp head`: mark a database created before Alembic (by the old `create_all`) as up to date
- `cd src && ../.venv/bin/python -m import_tasks sample_data/tasks.csv`: one-time import of tasks from a CSV into the `Task` table. Safe to re-run: existing task codes are skipped, never overwritten. Bad rows are reported and skipped. The sample file is made-up data; never commit a real tracker export.

## Layout
- `src/main.py`: FastAPI app (`uvicorn main:app`)
- `src/`: models, routes, templates, static files, tests
- `src/auth.py`: Google OAuth routes, environment-backed allowlist, signed-session identity, and the reusable route-protection dependency
- `src/board.py`, `src/templates/`: the `/tasks` board (Jinja2 templates; pages are server-rendered)
- `docs/design.md`: the design rules. `src/static/style.css` is the one stylesheet base.html links; `src/static/styles/tokens.css` holds every color, size, radius and font token; `src/templates/components/`: Jinja macros (status label, task card, notices, empty/error states, buttons, readout, gauge, coach message). Reuse them before adding markup. `tests/test_design_tokens.py` fails on raw hex or one-off sizes in the stylesheet
- `src/scripts/screenshots.py`: what `make screenshots` runs; `requirements-dev.txt`: dev-only tools (Playwright), not in the Docker image
- `src/coach.py`, `src/templates/task.html`: the `/tasks/{code}` page and its coach chat (htmx; `src/static/htmx.min.js` is vendored, v2.0.7, no CDN)
- `src/llm.py`, `src/prompts/coach_system.md`: model client, Drive tools (`drive_search` with a file-type filter, `drive_list_folder`, `drive_read_text`), the coach's system prompt. Progress steps go out through `run_coach(on_step=...)`; `coach.py` serves them at `/tasks/{code}/coach/progress` for the page to poll. Drive links in an answer that no tool returned are removed.
- `src/drive_index.py`: the local Drive index. `full_sync` / `incremental_sync` (Drive changes feed) read Docs (all tabs), Sheets (all tabs), PDFs (pypdf, skip over 20 MB) and CAD (name, folder and date only) into `DriveFile` / `DriveChunk` / `SyncState`; `search()` uses Postgres full text (`websearch_to_tsquery` + `ts_rank`) and falls back to LIKE on SQLite. Read-only Google scopes only. Triggers: `scripts/sync_drive.py` and the lead-only "Sync Drive now" button on `/admin`. The coach does not use it yet
- `src/markup.py`: coach answers are Markdown, rendered by markdown-it-py (raw HTML off) then sanitized with nh3 (allowlist; only https Google Docs/Drive links stay clickable). Model output is untrusted: never put it in a template unescaped except through `render_markdown` (the `markdown` filter)
- `evals/coach_questions.yaml`, `src/coach_eval.py`, `src/scripts/run_evals.py`: the coach's test questions, the scoring and report code (pure, unit-tested), and the `make eval` runner
- `src/import_tasks.py`, `src/sample_data/`: CSV parsing, subsystem/type guesses and saving (real tracker export or the made-up sample); also the CLI import
- `src/admin.py`, `src/templates/admin_import*.html`, `src/static/import.js`: lead-only `/admin/import` (upload, review and fix rows in place, then Import); leads are `LEAD_EMAILS`, everyone else gets 403. The review page is stateless: every edit re-posts the form and the server re-checks all rows
- `src/migrations/`: Alembic migrations (`versions/`); `src/alembic.ini` is the config
- `PROCESS.md`: design process log (written with the /reflect skill; don't edit it otherwise)
- `process/`: sketches and screenshots linked from PROCESS.md

## Rules
- Secrets live only in the environment: `GLASSROOM_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS` (path to a key file outside the repo) or `GOOGLE_SERVICE_ACCOUNT_JSON` (file contents, on Railway), `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`, and `DATABASE_URL`. Never write a secret into a committed file, a log line, or a test.
- The Google credential variable names are listed in `.env.example`; values stay in the local environment or Railway. Drive access is limited to files and folders shared with the service account.
- This repo is public. Never commit people's names, contact info, or exports of the tracker or Drive. Drive file names, catalog part numbers, and individual design values in eval questions are OK. Tests and seed data use made-up tasks and people.
- One task, one branch, one PR. Small commits. Never push to `main`; everything reaches `main` through a PR Samara reviews.
- Run /ship before opening any PR.
- Every model change needs a new Alembic migration. Never edit a migration that has been merged; add a new one. `tests/test_migrations.py` fails if the models and migrations disagree.
- Every LLM or Google call handles failure: the page says plainly what failed, and the rest of the page still works.
- Log every model call (model, input and output tokens, cost, task, member) to the database; this feeds the cost-benefit analysis.
- Pages must be semantic, accessible, honest about network errors, and usable at phone width (Lab 6 standard).

## Product rules the code must enforce
- The coach advises; it never decides. Leads approve drawings, routers, test plans, and decisions.
- The coach never states machining numbers (feeds, speeds, tap sizes, tolerances) without citing a source the member can open.
- Confirmed decisions in the database outrank Drive search results. If a Drive doc conflicts with a confirmed decision, flag it as possibly stale.
- Drive write-back is append-only: add a dated entry to the one doc mapped to a subsystem, after a member previews it and a lead approves it. Never edit or delete existing Drive content.
- Drive API calls on the team folder need `supportsAllDrives=True` and `includeItemsFromAllDrives=True` (it's a shared drive).
- No XP or points. Progress is shown as Crush's build progress and each member's completed tasks.

## Domain
- Crush: the AUV being built. Previous robot: Alfie (its docs are often stale for Crush).
- Subsystems: Enclosure, Seals/Waterproofing, Electronics Mounts, Thrusters, Gripper, Torpedo.
- Task IDs: M = mechanical, E = electronics mounts, A/G/T = actuators, gripper, torpedo (e.g. M04 "Finish enclosure wall panels").
- Task types and stages:
  - Fabricate: drawing → AI pre-check → lead review → process router → shop (machine) → inspect
  - Design: requirements → concepts → lead review → CAD → fit check
  - Validate: test plan → calculations → lead approval → run → log results
- Process router: the ordered list of machining operations for a part.

## Measuring the coach
- Run `make eval` before and after any coach change (`src/llm.py`, `src/prompts/coach_system.md`, `src/coach.py`, the Drive tools) and paste both summary lines in the PR description, e.g. `pass 11 / partial 3 / fail 2 | median 14.2s, max 31.0s | total cost $0.2210`. A change that makes the numbers worse needs a reason.
- It asks every question in `evals/coach_questions.yaml` the way the task page does (`coach.task_context` + `llm.run_coach`, empty chat history) using the real model and Drive. Needs `GLASSROOM_API_KEY` and the Google credentials; it refuses to start without them. A task code that isn't in the local database gets a made-up task, so answers can differ a little from the live page.
- Scoring (case-insensitive substring matches): **pass** = every `expect` appears, `cite` is the title of a source the answer links, no `must_not`; **partial** = at least half of `expect` and no `must_not`; **fail** = anything else, or an error/timeout.
- An `expect` item can be a list of alternatives (any one counts), for answers that can be worded several ways: `expect: [["not decided", "no decision"]]`.
- It waits 3 seconds between questions. A question that errors (rate limit, timeout, model error) is retried once after 20 seconds and the retry's cost counts. Errors still score as fail, but the summary line says how many fails were errors (`fail 5 (2 errors)`), and the report shows the server's log line for each, so provider flakiness isn't mistaken for a worse coach.
- It stops once the total cost passes $0.40 and says so (remaining questions show "not run"; also stops if Glassroom reports the budget is used up). The Glassroom cap is $1 per 5 hours for everyone, so don't run it twice in a row on the full set. Use `ONLY=<id>` or `KIND=<kind>` while iterating.
- Every answer is written to `.context/evals/<timestamp>.md` (to read) and `.json` (to compare runs); `.context/` is gitignored. Read the failing answers before changing anything: a wrong `expect` line is as likely as a wrong answer.

## Learned the hard way
(Add a line here whenever the agent makes the same mistake twice.)
- Behind Railway’s proxy, trust forwarded headers or generated URLs will be http.
