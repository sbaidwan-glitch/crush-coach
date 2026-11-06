# uv ships as a distroless image intended for exactly this: copy the binary out.
# Pinned, like everything else here — bump it deliberately.
FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

# Python 3.13 — the same version Lab 0 pinned, so Phase 1 work keeps running.
FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

# The upstream image ships an apt source for yarn whose signing key has since
# rotated, so a plain `apt-get update` dies with:
#
#   E: The repository 'https://dl.yarnpkg.com/debian stable InRelease' is not signed.
#
# We do not need that repository — yarn is already installed as a binary and
# Node comes from the devcontainer feature — so drop it before updating. This
# also removes a fragile third-party repo from the image the class depends on.
# Re-check this line if the base image is ever bumped; it may become unnecessary.
RUN rm -f /etc/apt/sources.list.d/yarn.list

# Command-line tools the Toolbelt videos assume are present.
#   vim               T3
#   less, tree, jq    T1
#   tmux              long-running processes; see note in INSTRUCTOR.md
#   sqlite3           class 10 (the database is a file)
#   postgresql-client class 12 (psql against Railway)
#   ripgrep           T1, and Claude Code's search
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      vim less tree jq tmux curl sqlite3 postgresql-client ripgrep \
 && rm -rf /var/lib/apt/lists/*

# uv — used by post-create.sh to install requirements, and available to students.
COPY --from=uv /uv /uvx /usr/local/bin/


# The native Claude Code installer puts the launcher here.
ENV PATH="/home/vscode/.local/bin:${PATH}"
