---
name: ship
description: Pre-PR checklist for Crush Coach. Confirms the GitHub issue, merges the latest origin/main, runs lint and tests with the workspace .venv, enforces migrations, lists the real-service checks the diff needs, checks UI at phone and desktop width, scans for secrets and real team data, then drafts and opens the PR.
argument-hint: "<issue number>"
disable-model-invocation: true
---

# /ship: everything I check before opening a PR

I run this before every PR. Each step exists because something went wrong without it.
The "Why" lines point at `PROCESS.md` (the 2026-11-08 entry) and the git history.

**Rules for this skill**
- Stop at the first failing step. Say what failed, show the output, and wait. Don't skip ahead and don't "fix and continue" past a merge conflict, a secret, or a missing migration without asking me.
- Never run bare `python`, `pytest`, `ruff`, or `pip`. Only `.venv/bin/...`.
- Never push to `main`, never merge the PR, never force-push.
- Never run the real-service checks (step 5) yourself: they cost money and touch the real Drive. I run them and paste the result.

## What changed

Gathered when I typed `/ship`:

```!
git fetch -q origin 2>/dev/null
echo "Issue argument: ${ARGUMENTS:-(none given)}"
echo "Branch: $(git status -sb | head -1)"
echo "Uncommitted:"; git status --short | head -20
echo "Behind origin/main by: $(git rev-list --count HEAD..origin/main 2>/dev/null) commits"
echo
echo "== Files changed vs origin/main =="
git diff --name-status origin/main...HEAD | head -80
echo
echo "== Commits on this branch =="
git log --oneline origin/main..HEAD | head -30
echo
echo "== Makefile has screenshots target? =="; grep -qE '^screenshots:' Makefile && echo yes || echo "no (not built yet)"
echo "== docs/design.md exists? =="; test -f docs/design.md && echo yes || echo "no"
true
```

## Steps

### 0. Clean start
If `git status --short` shows uncommitted changes, or the branch is `main`, stop and ask me.
Never sweep uncommitted work into the PR.

### 1. Confirm the issue
Run `gh issue view <n> --json number,title,state,url,body` with the number I gave. If I gave none, run `gh issue list` and ask me which one.

- The `url` must contain `/issues/`. If it contains `/pull/`, that number is a PR, not an issue: stop and tell me.
- `state` must be `OPEN`. If it is closed, stop and ask.
- Show me `#<n> <title>` and check the title against the diff. If it doesn't match what the branch does, stop and ask.
- Use that exact number in the PR body as `Closes #<n>`. Never take a number from memory, from an earlier PR, from the roadmap ("slice 9 of 10"), or from my prompt without this check.
- If there is genuinely no issue for this work, say so, write "No issue" in the PR instead of `Closes`, and don't invent a number.

*Why: GitHub issues and PRs share one numbering. "Issue #2" in my prompts was really #5, and PR #17 said "closes #9" using the roadmap slice number while the matching issue for slice 9 of the home page is #12.*

### 2. Include the latest origin/main
`git fetch origin`, then `git merge origin/main` (merge, not rebase, so I never have to force-push).
If it conflicts: run `git merge --abort`, list the conflicting files, and stop. I resolve those myself.
Do the later steps on the merged result, so what I test is what will land.

*Why: main moves while a branch is open. Tests that pass on a stale branch say nothing about the merged result, which is what the PR environment builds.*

### 3. Lint and tests, with the workspace .venv
Run both, in this order, and keep the output:
```
.venv/bin/ruff check src
make test
```
`make test` runs `../.venv/bin/pytest -q` from `src/`. If `.venv/bin/python` doesn't exist, create it first: `python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
Any failure stops the skill. Don't edit tests to make them pass without telling me why.

*Why: system Python isn't the pinned environment from `requirements.txt`, so a pass or fail there says nothing about the app. Commit 1cb4e31 moved every command to `.venv`. CI runs the same two commands, so a PR that fails them locally fails there too.*

### 4. Models need a migration
If `src/models.py` is in `git diff --name-only origin/main...HEAD`:
- `git diff --diff-filter=A --name-only origin/main...HEAD -- src/migrations/versions` must list at least one new file. If not, stop: tell me to run `cd src && ../.venv/bin/alembic revision --autogenerate -m "describe change"` (needs `DATABASE_URL` at head) and review the file.
- `git diff --diff-filter=MD --name-only origin/main...HEAD -- src/migrations/versions` must be empty. A merged migration is never edited; if one shows up, stop.
- `tests/test_migrations.py` (already part of step 3) must pass.

*Why: CLAUDE.md rule, and the app runs `upgrade head` on startup and only logs a failure, so a missing migration doesn't crash anything. It shows up later as missing columns on Railway.*

### 5. Real-service checks (mocks aren't enough)
List the changed files that match, and give me the exact command for each. Only list the rows that match the diff, and say which file triggered each.

| Diff touches | I run this | Pass looks like |
|---|---|---|
| `src/drive.py` | `cd src && ../.venv/bin/python scripts/drive_search.py "latch"` | Real results from the shared drive, including older folders, and no `DriveError`. Also check the diff for `orderBy` (`git diff origin/main...HEAD -U0 -- src/drive.py \| grep -n orderBy`): it must not be there; sort in Python. |
| `src/llm.py` | `cd src && ../.venv/bin/python scripts/ask_coach.py "What latch did we choose, and how is it mounted?"` | A final answer with Drive links, not "Let me check…". `tool calls:` is 4 or fewer, and every `call N:` line says `ok`. Paste the whole output. |
| `src/coach.py` | `docker compose up -d`, then `make dev`, sign in, open a task page such as `/tasks/M04`, send one question | The answer appears on the page, and a row is added to `llm_call`: `docker compose exec db psql -U e190bo -d app -c "select model, status, input_tokens, output_tokens, cost from llm_call order by id desc limit 1"`. Also run the `llm.py` command above. |
| `src/auth.py` or `Dockerfile` | `docker build -t crush-coach-ship . && docker run --rm -p 8000:8000 --env-file .env crush-coach-ship`, then in another terminal `curl -si -H 'X-Forwarded-Proto: https' localhost:8000/login \| grep -i '^location:'` | The `redirect_uri=` in that header starts with `https%3A%2F%2F`. On the PR environment, run `curl -si https://<pr-env-host>/login \| grep -i '^location:'`: same check, and that exact `/auth/callback` URL must be an authorized redirect URI on the Google OAuth client. |

If the row list isn't empty:
1. Add the label `needs real check` (`gh label create "needs real check" --color FBCA04 --description "Real-service check not yet pasted" 2>/dev/null; gh pr edit --add-label ...` once the PR exists).
2. Put `**Status: needs real check. Don't merge until the result is pasted below.**` at the very top of the PR body.
3. The status stays that way until I paste the output. When I do, replace the banner with the pasted output (check it for keys first), update the body with `gh pr edit --body-file`, and remove the label with `gh pr edit --remove-label "needs real check"`. If my output shows a failure, the status stays and we fix it.

If no row matches, write "No real-service check needed: diff doesn't touch drive.py, llm.py, auth.py, coach.py or the Dockerfile" in the PR.

*Why: mocked tests passed twice while the real thing failed.*
- *`drive.py`: Google rejects `orderBy` combined with full-text search. The mocked Drive accepted it; the real call errored (fixed in 582d698).*
- *`auth.py`, `Dockerfile`: behind Railway's proxy the callback URL was built as `http`, which would have broken sign-in (fixed in 15a2688). Tests can't see the proxy.*
- *`llm.py`, `coach.py`: the coach spent all 4 tool calls reading long docs and ended with "Let me check…" and no answer, for about $0.027. Only a real question shows that.*

### 6. UI changes: check phone and desktop width
If the diff touches `src/templates/` or any CSS under `src/static/` (not the vendored `htmx.min.js`):
- If the Makefile has a `screenshots` target (see "What changed" above), run `make screenshots`. If it doesn't, **say it doesn't exist yet** and don't invent it. Ask me to check 375px and 1280px in the browser, and list the check under "what wasn't verified" until I confirm.
- Look at the screenshots at **375px** and **1280px** (open the image files). At 375px: no horizontal scroll, text readable, tap targets usable. At 1280px: layout holds up and isn't stretched.
- Compare against `docs/design.md`. If that file doesn't exist, say so, use the Lab 6 standard from CLAUDE.md (semantic, accessible, honest about network errors, usable at phone width), and list "no design doc to check against" under what wasn't verified. Don't create `docs/design.md`.
- Screenshots may show real team data. This repo is public, so don't commit them or link them in the PR unless the page was running on made-up data.

*Why: CLAUDE.md sets phone width as the standard, and a desktop-only check misses the most common break.*

### 7. Scan for secrets and real team data
Scan every commit on the branch, not just the final diff: a key that was added and then deleted is still in public history.

```
git diff --name-only origin/main...HEAD | grep -E '(^|/)\.env($|\.local$)|-key\.json$|\.pem$|\.bundle$|\.(db|sqlite3?)$'
git log origin/main..HEAD --name-only --format= | grep -E '(^|/)\.env($|\.local$)|-key\.json$|\.pem$|\.bundle$|\.(db|sqlite3?)$'
git log origin/main..HEAD -G'-----BEGIN [A-Z ]*PRIVATE KEY|"private_key"|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|GOCSPX-|ya29\.|(GLASSROOM_API_KEY|SESSION_SECRET|GOOGLE_OAUTH_CLIENT_SECRET|GOOGLE_SERVICE_ACCOUNT_JSON)=[^ ]' --oneline --name-only
git log origin/main..HEAD -G'postgres(ql)?://[^:/ ]+:[^@ ]+@|(docs|drive)\.google\.com/[^ ]*[A-Za-z0-9_-]{25,}' --oneline --name-only
```
`.env.example` is allowed. Its `postgresql://e190bo:localdev@localhost` URL is the local-dev default and is fine. `-G` lists the commit and file only, never the matching text, so the output can't leak the secret. **Never print the secret itself.** Report commit, file, and line number only.
A match isn't automatically a leak: open the file and look. The tests deliberately contain dummy strings like `postgresql://user:secret@host/db` (they check that error messages don't leak credentials), and those are fine.

Then read the added lines in tests, `src/sample_data/`, `src/templates/`, `src/prompts/`, scripts, and docs, and look for real team data: member names or emails (made-up ones use `example.com`), Drive links or file IDs, real part numbers or file names from the Drive, tracker exports, and CSVs that aren't the made-up sample. Tests and seed data use made-up tasks and people only.

If anything turns up: stop before pushing. Show me the commit, file, and line (not the value), and ask how to proceed. Removing it from history is my decision, and if it was already pushed anywhere, the key must be rotated.

*Why: the repo is public and the coach reads the team's real Drive, so real file names and links can reach a test fixture by accident. CLAUDE.md: never commit a secret, a Drive export, or real team data.*

### 8. Write the PR description, then open the PR
Write the body to a temp file **outside the repo** (so quotes can't break the command). Sections, in this order:

```markdown
**Status: needs real check. Don't merge until the result is pasted below.**   <- only if step 5 listed any row

Closes #<n>      <- the number confirmed in step 1, or "No issue"

## Plan
What this PR sets out to do and how, in my terms. If the issue has a "Done means" list, go through it item by item and say how each is met, or that it isn't.

## Test output
Verbatim tail of `.venv/bin/ruff check src` and `make test` from step 3 (the pass/fail summary lines), plus the migration result if step 4 applied.

## Real-check status
One line per row from step 5: the file that triggered it, the command, and either "pasted below" with the output or "not run yet". Or "No real-service check needed" with the reason.

## What wasn't verified
Everything skipped or only partly checked, plainly. Examples: real checks not yet run, `make screenshots` missing, no `docs/design.md`, widths not looked at, behaviour only covered by mocks, Railway PR environment not opened.
```

Show me the title and body and **wait for my OK**.
Then: `git push -u origin HEAD` (never `main`), `gh pr create --base main --title "<title>" --body-file <temp file>`, and add the `needs real check` label if step 5 listed any row.
Give me the PR URL, and remind me to read it and merge it myself. Don't merge it.
