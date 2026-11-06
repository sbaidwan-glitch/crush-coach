#!/usr/bin/env bash
# Runs once, when the container is first created.
#
# Note on ordering: devcontainer *features* (node, docker-in-docker, gh) are
# applied AFTER the Dockerfile, so anything that depends on them has to live
# here rather than in the image build. That is why the Railway CLI is installed
# at this stage and not with apt.
set -euo pipefail

echo "==> Installing Python packages (uv)"
# uv resolves this set in ~1s vs ~8s for pip, with an identical 53-package result.
#
# Flags, because two obvious ones are wrong here:
#   --system  : install into the real interpreter, not a venv. WITHOUT sudo this
#               fails — /usr/local/lib/python3.13/site-packages is root-owned, and
#               `uv pip install --system --dry-run` does NOT catch that.
#   sudo      : hence. Passwordless sudo is available in this image.
#   --user    : uv rejects it outright ("pip's `--user` is unsupported").
sudo uv pip install --system --no-cache -r requirements.txt

echo "==> Registering the notebook kernel"
# JupyterLab started with `uvx jupyter lab` runs in uv's own environment, whose
# default kernel can't see the packages installed above (sqlmodel, jupysql...).
# This registers this codespace's Python as a kernel it can see, listed as
# "Python (codespace)". Harmless if it's already there.
python -m ipykernel install --user --name e190bo --display-name "Python (codespace)"

echo "==> Installing Railway CLI"
# Pinned, same rule as requirements.txt. Bump deliberately, not by accident.
npm install -g --silent @railway/cli@5.58.0

echo "==> Installing Claude Code"
# The native installer is the method the docs recommend, and it self-updates in
# the background. If you need a version pinned for the whole class instead, the
# signed apt repository is the alternative:
#   https://code.claude.com/docs/en/setup#install-with-linux-package-managers
curl -fsSL https://claude.ai/install.sh | bash

echo "==> Warming bytecode caches"
# jupysql emits SyntaxWarnings the first time Python compiles it. That first time
# would otherwise be a student running Part 1 of Lab 5, so absorb it here where
# nobody is reading. Harmless either way, but it looks like a broken environment.
# Needs sudo: site-packages is root-owned, so a non-root warm-up cannot write
# the __pycache__ files and the warning returns on the student's first import.
sudo python -c "import sql, pandas, httpx, fastapi" 2>/dev/null || true

echo "==> Environment file"
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "    created .env from .env.example"
fi

cat <<'BANNER'

  ─────────────────────────────────────────────
   E190BO environment ready.

   bash demos/check_tools.sh     is everything installed?
   python demos/hello_web.py     does port forwarding work?
   More checks: demos/README.md

   Your lab's code goes in src/. Run it from there:
     cd src && uvicorn main:app --reload
   Then open port 8000 from the PORTS tab.
  ─────────────────────────────────────────────

BANNER
