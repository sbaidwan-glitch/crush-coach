#!/usr/bin/env bash
# Is everything installed? Prints one line per tool, with its version.
#
#   bash demos/check_tools.sh
#
# This only checks that each tool *starts*. The other demos in this folder
# check that the tools actually *work*.

# The checks only mean something inside the container. Run on a laptop, they
# describe the laptop — e.g. macOS has no `python` command, only `python3`.
if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ] && [ -z "${CODESPACES:-}" ]; then
  echo "⚠️  You are not inside the dev container, so this is checking your own"
  echo "   computer, not the course environment. Open a terminal in VS Code"
  echo "   while connected to the codespace or dev container, and run it there."
  echo
fi

failed=0

check() {
  local name=$1; shift
  local out
  if ! command -v "$1" >/dev/null; then
    printf "  ❌ %-16s %s: command not found\n" "$name" "$1"
    failed=1
  elif out=$("$@" 2>&1); then
    printf "  ✅ %-16s %s\n" "$name" "${out%%$'\n'*}"   # first line only
  else
    printf "  ❌ %-16s %s\n" "$name" "${out##*$'\n'}"   # last line: usually the error
    failed=1
  fi
}

echo "Tools"
check python          python --version
check uv              uv --version
check git             git --version
check gh              gh --version
check node            node --version
check vim             vim --version
check tmux            tmux -V
check jq              jq --version
check tree            tree --version
check ripgrep         rg --version
check sqlite3         sqlite3 --version
check psql            psql --version
check docker          docker version --format 'client {{.Client.Version}}, server {{.Server.Version}}'
check "docker compose" docker compose version
check railway         railway --version
check claude          claude --version

echo
echo "Python packages"
check "requirements"  python -c "import fastapi, uvicorn, sqlmodel, alembic, psycopg, dotenv, pandas, pytest, httpx, mcp; print('all import')"

echo
if [ "$failed" -eq 0 ]; then
  echo "Everything is installed."
else
  echo "Something above is missing or broken."
  echo "If you are inside the container, try: Command Palette → Rebuild Container."
  exit 1
fi
