# student-template — instructor notes

Authoring copy of the repo students start from: one new repo per lab for Labs 5–7
(lab code in `src/`), and the project.

**Recommendation: move this to its own repo and make that the source of truth.**
See "Publishing" below for why, and for the extraction command.

Rationale for the whole approach: `docs/tooling-decision.md`.

## Use this template — never fork

Students must use **Use this template**, not **Fork**. This is not a style
preference:

| | Fork | Use this template |
|---|---|---|
| Visibility | **Tied to the upstream network.** Fork a public repo and the student's copy is *forced public* | Independent — a **public template can produce private student repos** |
| Discoverability | Listed in the upstream fork network; classmates can read each other's work | Not linked to anything |
| History | Inherits the full upstream history | **One fresh commit** |
| Contribution graph | Fork commits do **not** count | Commits **do** count |

So the combination you want is a **public template repo** and **private student
repos created from it**. That is only reachable through "Use this template".

The clean history matters for grading too: Lab #5 asks for "at least 5 meaningful
commits", which is only measurable if the student's history starts at commit one
rather than inheriting yours.

**The repo must be created in their personal account, not a course org.**
Codespaces bills the repo owner. Personal account → their own GitHub Education
allowance (180 core-hours/month, free to you). An org → the org pays. Say this
explicitly in the handout; the "Use this template" dialog has an owner dropdown
and students will not think about it.

## Publishing

Push the **contents** of this directory as the root of a new repo, so
`.devcontainer/` sits at that repo's root where Codespaces looks for it.

This directory is also used as a private scratch repo for testing, and that
repo's history contains these instructor notes. So do **not** flip the test repo
to a template — publish from a clean copy with fresh history:

```bash
rm -rf /tmp/e190bo-template
cp -R . /tmp/e190bo-template            # run from this directory
cd /tmp/e190bo-template
rm -rf .git                             # drop the test repo's history
rm -f INSTRUCTOR.md TESTING.md          # instructor-only — do not ship these
rm -f .env                              # build artifact; .env.example is the shipped one

git init -b main
git add -A && git commit -m "E190BO course template"
gh repo create <you>/e190bo-fa26-template --public --source=. --push
gh repo edit <you>/e190bo-fa26-template --template
```

Verify the template flag took: the repo page should show a green **Use this
template** button.

**Keep `.devcontainer/devcontainer-lock.json`.** The devcontainer CLI generates
it and it pins all three features to exact SHA digests (docker-in-docker 2.17.0,
github-cli 1.1.2, node 1.7.1). Without it, `:2` and `:1` resolve to whatever is
newest the day a student builds, and two students who start a week apart get
different environments. Shipping it is the same discipline as pinning
`requirements.txt` — commit it, and bump it deliberately by re-running the
devcontainer CLI.

### Which repo is the source of truth?

Once students have created repos from it, **the standalone repo should be the
source of truth** and this directory should go away. Two reasons:

1. **Drift.** You will iterate on the template from inside a codespace and
   commit there. This copy then goes stale, and eventually someone re-publishes
   the stale one over the good one.
2. **`INSTRUCTOR.md` and `TESTING.md` must not ship to students**, so the two
   trees are not identical anyway — which is exactly the condition that makes a
   mirror unsafe.

Move those two files to `docs/` in the course repo and delete
`student-template/`. The course repo keeps the *reasoning* and the *testing
procedure*; the template repo keeps the *code*.

## Tooling manifest

Everything the unit needs, where it comes from, and which class first requires
it. Audited inside the built container, not assumed.

### Already in the base image — nothing to do

`mcr.microsoft.com/devcontainers/python:1-3.13-bookworm` ships more than you
would expect. Verified present: `bash` `git` `curl` `wget` `ssh` `nano`
`make` `gcc` `htop` `zip`/`unzip`, and **working `man` pages** (not stripped,
which many slim images do — `man ls` works, so T1 can teach it).

### Added by us

| Layer | What | For |
|---|---|---|
| Dockerfile (apt) | `vim` | T3 |
| Dockerfile (apt) | `less` `tree` `jq` | T1 |
| Dockerfile (apt) | `tmux` | T1 — instructor preference; see note below |
| Dockerfile (apt) | `sqlite3` | class 10 — the database is a file |
| Dockerfile (apt) | `postgresql-client` (`psql`) | class 12 — talk to Railway's Postgres |
| Dockerfile (apt) | `ripgrep` | T1, and Claude Code's search |
| Dockerfile (`COPY --from`) | `uv` / `uvx` 0.12.17 | fast installs; see note below |
| Feature | `docker-in-docker` → Docker + **Compose v2.40.3** | T4, class 12 |
| Feature | `github-cli` (`gh`) | T2 |
| Feature | `node:22` | class 14 MCP Inspector, Railway CLI |
| post-create | `@railway/cli@5.58.0` | T5, class 12, Lab #7 |
| post-create | Claude Code (native installer) | classes 13, 14, Lab #7 |
| requirements.txt | fastapi, uvicorn, sqlmodel, alembic, psycopg | classes 10, 12 |
| requirements.txt | python-dotenv | class 12 |
| requirements.txt | pytest, httpx, ruff | class 16 |
| requirements.txt | mcp 2.2.0 | class 14 |

### VS Code extensions

| Extension | For |
|---|---|
| `ms-python.python` | the debugger, class 16 |
| `charliermarsh.ruff` | linting, class 16 |
| `ms-azuretools.vscode-docker` | class 12 |
| `humao.rest-client` | `.http` files — Lab #5 asks for one |
| `esbenp.prettier-vscode` | class 11 |
| `qwtel.sqlite-viewer` | class 10 — lets students *see* the database they made |
| `github.vscode-github-actions` | class 16 — CI |
| `ms-toolsai.jupyter` | lab notebooks (`hw5.ipynb`, …) |

### Coverage check, class by class

| Class / video | Needs | Status |
|---|---|---|
| T1 terminal | bash, less, tree, jq, grep, find, man | ✅ |
| T2 git | git, gh | ✅ |
| T3 vim | vim | ✅ |
| T4 docker | a real daemon, compose | ⚠️ daemon starts; `docker run` unproven on Codespaces |
| T5 railway | Railway CLI | ✅ |
| 9 full-stack | curl, a browser | ✅ |
| 10 backend | Python 3.13, FastAPI, SQLite | ✅ |
| 11 frontend | static server, port forwarding | ✅ `python -m http.server` |
| 12 deploy | Docker, Postgres, psql, Railway | ⚠️ same docker caveat |
| 13 prototyping | Claude Code, git | ⚠️ needs a paid account |
| 14 MCP | `mcp`, Node for the Inspector | ✅ |
| 15 project launch | — | n/a |
| 16 dev tools | pytest, ruff, debugger, Actions | ✅ |

### tmux

Included at the instructor's request. Worth knowing the tradeoff either way:
VS Code already splits terminals with a click, so for a beginner tmux is a second
way to do something they can already do, with a modal keybinding layer on top.

Where it genuinely earns its place: a student SSH'd into a codespace from a bare
terminal, and any long-running process that must survive a dropped connection.
If it goes in T1, give it ninety seconds at the end — `tmux`, `Ctrl-b d`,
`tmux attach` — and frame it as "for when you are not in VS Code," not as the
normal way to work. Teaching it as the default will cost more than it returns.

### uv

`uv` and `uvx` are on the PATH, pinned to 0.12.17 via `COPY --from` out of the
official distroless image (the method Astral documents for containers).

**Measured on this requirements set, inside the container:**

| | cold cache | warm cache |
|---|---|---|
| `pip` | 8 s | — |
| `uv` | **1 s** | **<1 s** |

Identical resolution — 53 packages both ways. `post-create.sh` now uses uv.

**Two flags that look right and are not**, both found by testing rather than
reading:

- `uv pip install --system` **fails** as the `vscode` user —
  `/usr/local/lib/python3.13/site-packages` is root-owned, `Permission denied`.
  Worse, `--dry-run` reports success, because it never checks write access. The
  call needs `sudo`. (Plain `pip` silently falls back to a `--user` install,
  which is why this never surfaced before.)
- `uv pip install --user` is **rejected outright**: *"pip's `--user` is
  unsupported (use a virtual environment instead)."*

So the working form is `sudo uv pip install --system`. A side effect worth
knowing: packages now live in `/usr/local/lib/python3.13/site-packages` rather
than `~/.local/...`, which is the more conventional place and makes tracebacks
less confusing.

**Two decisions to keep separate:**

1. **Using uv to build the container** — pure win, invisible to students, and it
   cuts the cold-start time that decides whether they must pre-build before 9/30.
2. **Teaching uv to students** — a real curriculum choice. `env/SETUP.md` taught
   `python3.13 -m venv` + `pip install -r requirements.txt` in week 1, and every
   lab so far has used it. Introducing a second package manager at class 9 is a
   new tool to learn during the densest stretch of the course.

The safe framing: uv is what builds the environment, `pip` still works exactly as
they learned it, and uv is there for anyone who wants it. If you do want to teach
it, T1 is the place — not class 10, where the lesson is supposed to be HTTP.

### Deliberately left out

- **Live Server** — `python -m http.server` is already documented for class 11,
  and it teaches that the page is *served* rather than opened. Two ways to do
  one thing is worse than one slightly manual way.
- **`requests`** — `httpx` is already present as a FastAPI test dependency and
  does the same job. Students reaching for `requests` out of habit is a teaching
  moment, not a gap.
- **Jinja2 / server-side templates** — the unit deliberately separates a JSON API
  from a static frontend. Adding templates invites a third architecture.
- **`act`** (run GitHub Actions locally) — class 16 pushes to real CI, which is
  the point.

## Build status

**Built and smoke-tested 2026-09-21** (five builds, three real bugs found) with `@devcontainers/cli up` against a real
Docker daemon. Outcome `success`. Verified *inside* the running container:

| | |
|---|---|
| Python | 3.13.5 — matches the Lab 0 pin |
| git / gh | 2.50.1 / 2.101.0 |
| vim, less, tree, jq, sqlite3, psql, rg, man | all present and working |
| Node | 22.23.2 |
| Docker Compose | v2.40.3 |
| Railway CLI | 5.58.0 |
| Claude Code | 2.1.278 — installs and runs |
| tmux | 3.3a |
| uv / uvx | 0.12.17 — **installed 53 packages in 54 ms** in the real build |
| ruff / alembic | 0.16.8 / 1.20.0 |
| MCP | `from mcp.server.mcpserver import MCPServer` imports |

And the real class shapes, not just imports:

- **SQLModel round trip** against SQLite — create table, insert, select. ✅
- **FastAPI boots under uvicorn**, serves `GET /ideas`, and `/docs` returns 200
  — so the class 10 activity works as designed. ✅
- **`python -m http.server`** serves a file on a forwarded port — class 11. ✅

## Known gaps — resolve before 9/30

1. **docker-in-docker is unproven on Codespaces.** In the test container the
   daemon *does* start (dockerd running, server 29.8.1) and the container is
   privileged, so the feature installed correctly — but `docker run hello-world`
   failed:

   ```
   iptables v1.8.9 (legacy): can't initialize iptables table `raw':
   Table does not exist (do you need to insmod?)
   ```

   That is the **host kernel** of the sandbox it was built in lacking the
   netfilter `raw` table, not a fault in this configuration. Codespaces runs a
   normal Ubuntu host where docker-in-docker is a supported feature, so this
   very probably works there — but *probably* is not good enough for the class
   12 activity. **Open a real codespace and run `docker run hello-world`.**
   This is the single most important check remaining.

2. ~~Railway CLI~~ — **done**, `@railway/cli@5.58.0` pinned in `post-create.sh`.
   Installing it does not decide whether T5 *teaches* the dashboard or the CLI;
   both are now available, so that stays a pedagogy call rather than a blocker.

3. **Claude Code needs a paid account.** The binary installs fine, but the docs
   are explicit: Pro, Max, Team, Enterprise, or Console — **the free claude.ai
   plan does not include Claude Code**. Classes 13 and 14 and Lab #7 assume
   every student can run it. A budget and access question, not a technical one,
   and the biggest open risk in the unit.

4. **Cold build time not measured on Codespaces hardware.** It was ~90 seconds
   here on a warm image cache. A student's first build on a 2-core codespace
   will be slower. Measure it; if it is over ~5 minutes, either turn on
   prebuilds or make "create your codespace" explicit before-class work for 9/30.

## Testing it

Push a scratch repo from this directory, open a codespace, and run the class 10
activity start to finish. Then the class 12 one, which is the real test —
docker-in-docker is the part most likely to surprise.
