# One documented way to run and submit this project.
# `make` with no arguments lists the targets.

# The newest tag in this repo, e.g. lab-06. Override with TAG=...
TAG ?= $(shell git describe --tags --abbrev=0 2>/dev/null)

.DEFAULT_GOAL := help
.PHONY: help dev test submit

help:  ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk -F':.*?## ' '{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# The lab's code lives in src/, so both run from there.
dev:   ## run the API on :8000 (from src/)
	cd src && uvicorn main:app --reload --host 0.0.0.0 --port 8000

test:  ## run the tests (from src/; start the API first)
	cd src && pytest -q

# Package a tagged snapshot for Canvas.
#
# Produces a git *bundle*, not a zip: one file holding the repository AND its
# history up to the tag, so your instructor can `git clone` it and read your
# commits. A zip would carry only the files.
#
# Only committed work is included — that is the point. Your .env and your
# database are gitignored, so they cannot leak into a submission by accident,
# and work you did after the tag (next week's lab) is not in it either.
#
# Why the temporary worktree: `git clone` needs a HEAD to check out. A bundle
# built from a tag alone has no HEAD, and clones into an empty directory that
# looks like you submitted nothing. Checking the tag out in a throwaway worktree
# puts HEAD at the right commit without touching your working tree.
submit:  ## package a tagged snapshot for Canvas (newest tag, or TAG=lab-06)
	@test -n "$(TAG)" || { echo "No tags yet. Tag the commit you're submitting first:  git tag lab-06"; exit 1; }
	@git rev-parse -q --verify "refs/tags/$(TAG)" >/dev/null \
	  || { echo "No tag '$(TAG)'. Create it first:  git tag $(TAG)"; exit 1; }
	@test -z "$$(git status --porcelain)" \
	  || echo "⚠️  Uncommitted changes are NOT in the bundle. Commit, move the tag, re-run."
	@rm -rf .submit-wt "$(TAG).bundle"
	@git worktree add --detach .submit-wt $(TAG) >/dev/null 2>&1
	@git -C .submit-wt bundle create "$(CURDIR)/$(TAG).bundle" HEAD >/dev/null 2>&1
	@git worktree remove --force .submit-wt >/dev/null 2>&1
	@echo
	@echo "✅ Wrote $(TAG).bundle ($$(du -h $(TAG).bundle | cut -f1)), $$(git rev-list --count $(TAG)) commits"
	@echo "   Upload it to Canvas with your executive summary."
	@echo "   Right-click it in the VS Code explorer → Download."
	@echo
	@echo "   Verify it before you submit:"
	@echo "     git clone $(TAG).bundle /tmp/check && ls /tmp/check"
