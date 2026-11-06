# Testing the devcontainer

Three ways, cheapest first. Do all three before 9/30 — they test different things,
and only the third tests what students will actually experience.

---

## 1. The fast loop — devcontainer CLI, no GitHub, no VS Code

This is the one to use while iterating on `devcontainer.json`. It builds the
image, applies the features, and runs `post-create.sh` exactly as Codespaces
would, straight from a terminal.

Needs Docker running and Node 18+.

```bash
cd student-template
npx @devcontainers/cli up --workspace-folder .
```

Then get a shell inside it:

```bash
npx @devcontainers/cli exec --workspace-folder . bash
```

Tear down and rebuild from scratch (do this after *any* edit to
`devcontainer.json` or the `Dockerfile` — a warm cache hides broken layers):

```bash
docker rm -f $(docker ps -aq --filter "label=devcontainer.local_folder=$PWD") 2>/dev/null
npx @devcontainers/cli up --workspace-folder . --remove-existing-container
```

**What this catches:** apt packages, pip resolution, the Claude Code installer,
feature conflicts, `post-create.sh` bugs. Most of your risk.

**What it misses:** VS Code extensions, port forwarding UI, Codespaces billing
and cold-start time.

---

## 2. Local VS Code — the "student on a laptop" path

Install [VS Code](https://code.visualstudio.com/) and the **Dev Containers**
extension, open `student-template/`, then **Dev Containers: Reopen in Container**.

**What this catches:** whether the extension list installs cleanly, whether
`forwardPorts` surfaces the Ports panel, whether `formatOnSave` fights anything.
This is also exactly the escape-hatch path documented in `README.md`, so it needs
to work.

---

## 3. A real Codespace — the only honest test

Push this directory to a scratch GitHub repo and open a codespace on it.

```bash
cd student-template
git init && git add -A && git commit -m "devcontainer test"
gh repo create <you>/e190bo-devcontainer-test --private --source=. --push
gh codespace create --repo <you>/e190bo-devcontainer-test
gh codespace ssh
```

**What only this catches:**

- **Cold build time.** Time it. If it is over ~5 minutes, students must create
  their codespace *before* class on 9/30, and the handout has to say so in bold.
  Consider a [prebuild](https://docs.github.com/en/codespaces/prebuilding-your-codespaces)
  if it is bad.
- **Core-hour burn.** Check usage after an hour of realistic work and multiply.
- **The 2-core machine.** Codespaces defaults are smaller than your laptop.
  `pip install` and `docker build` are both slower there.
- **Whether `docker-in-docker` actually works in Codespaces**, which is the
  single most important thing to verify, because class 12 depends on it.

Delete it when done — `gh codespace delete` — or it bills storage.

---

## Smoke test — run inside the container

Maps to the classes that depend on each item.

```bash
# --- toolbelt ---
python --version                 # 3.13.x        (Lab 0 parity)
git --version && gh --version    # T2
vim --version | head -1          # T3
tmux -V                          # T1
docker run --rm hello-world      # T4, class 12  ← the one that matters
claude --version                 # class 13, 14
railway --version                # T5, class 12
uv --version                     # used by post-create

# --- class 10: backend ---
python -c "import fastapi, sqlmodel, alembic; print('api ok')"
sqlite3 :memory: "select 1;"

# --- class 11: frontend ---
python -m http.server 8080 --directory . &
curl -sS localhost:8080 >/dev/null && echo "static ok"; kill %1

# --- class 12: deployment ---
docker compose up -d
psql "postgresql://e190bo:localdev@localhost:5432/app" -c "select version();"
docker compose down -v

# --- class 14: MCP (note the v2 name) ---
python -c "from mcp.server.mcpserver import MCPServer; print('MCPServer ok')"
```

**The `docker run hello-world` line is the load-bearing one.** Everything else
degrades gracefully; if docker-in-docker fails, class 12 has no activity.

---

## Issues already found and fixed

Keep this list — each one is a build that would have failed in front of the class.

### The base image's yarn apt source breaks `apt-get update`

**Symptom:** the image build dies on the first `RUN apt-get install` with

```
E: The repository 'https://dl.yarnpkg.com/debian stable InRelease' is not signed.
```

**Cause:** `mcr.microsoft.com/devcontainers/python:1-3.13-bookworm` ships
`/etc/apt/sources.list.d/yarn.list`, and that repository's signing key has since
rotated. `apt-get update` treats an unsigned repo as fatal, so every downstream
`apt-get install` fails — nothing to do with the packages we asked for.

**Fix (already in the Dockerfile):** `rm -f /etc/apt/sources.list.d/yarn.list`
before updating. We do not need it — yarn is already present as a binary and
Node comes from the devcontainer feature.

**Re-check this** if the base image tag is ever bumped; upstream may fix it, at
which point the line becomes harmless but unnecessary.

### The MCP SDK renamed `FastMCP` to `MCPServer`

**Symptom:** class 14 code copied from any tutorial fails with

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

**Cause:** `mcp` 2.x renamed it. Use `from mcp.server.mcpserver import MCPServer`.
`requirements.txt` pins `mcp==2.2.0`, so this is the version students get.
Covered in `lectures/lecture-14-mcp-server/README.md`.

---

## Then run a real class activity

Passing a smoke test is not the same as the environment being usable. Before
9/30, build the class 10 activity (the idea-board API) start to finish inside
the container, and the class 12 one after that. Those two are where the
environment either holds up or does not.
