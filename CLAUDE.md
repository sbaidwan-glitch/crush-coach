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
- `.venv/bin/ruff check src`: lint
- `cd src && ../.venv/bin/python scripts/drive_search.py "latch"`: search the team shared drive (metadata only)
- `cd src && ../.venv/bin/python scripts/ask_coach.py "question"`: ask the coach one real question (real model and Drive; no database)
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
- `src/coach.py`, `src/templates/task.html`: the `/tasks/{code}` page and its coach chat (htmx; `src/static/htmx.min.js` is vendored, v2.0.7, no CDN)
- `src/llm.py`, `src/prompts/coach_system.md`: model client, Drive tools, the coach's system prompt
- `src/import_tasks.py`, `src/sample_data/`: CSV parsing, subsystem/type guesses and saving (real tracker export or the made-up sample); also the CLI import
- `src/admin.py`, `src/templates/admin_import*.html`: lead-only `/admin/import` (upload, preview, then Import); leads are `LEAD_EMAILS`, everyone else gets 403
- `src/migrations/`: Alembic migrations (`versions/`); `src/alembic.ini` is the config
- `PROCESS.md`: design process log (written with the /reflect skill; don't edit it otherwise)
- `process/`: sketches and screenshots linked from PROCESS.md

## Rules
- Secrets live only in the environment: `GLASSROOM_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS` (path to a key file outside the repo) or `GOOGLE_SERVICE_ACCOUNT_JSON` (file contents, on Railway), `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`, and `DATABASE_URL`. Never write a secret into a committed file, a log line, or a test.
- The Google credential variable names are listed in `.env.example`; values stay in the local environment or Railway. Drive access is limited to files and folders shared with the service account.
- This repo is public. Never commit real team data (Drive exports, member names, the real tracker). Tests and seed data use made-up tasks and people.
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

## Learned the hard way
(Add a line here whenever the agent makes the same mistake twice.)
- Behind Railway’s proxy, trust forwarded headers or generated URLs will be http.
