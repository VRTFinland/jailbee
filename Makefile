.PHONY: install install-gui install-skill check build publish-testpypi \
        public-push changelog release

# Use bash for recipe lines — Typer's --show-completion uses
# shellingham, which inspects the parent process; under the default
# /bin/sh make would emit "Shell sh not supported."
SHELL := /bin/bash

# Install/reinstall the `jailbee` CLI globally from the current checkout WITH
# the optional Qt GUI extra (PySide6). Also (re)installs the user systemd
# timer that refreshes the egress pool and bash completion. `jailbee net
# install` is idempotent, so re-running this target is safe. Requires libGL
# on the host; `jailbee gui` and `jailbee dashboard --gui` work after this.
install: install-skill
	uv tool install '.[gui]' --force --reinstall
	jailbee net install
	mkdir -p ~/.local/share/bash-completion/completions
	@for name in jailbee jb; do \
		$$name --show-completion > ~/.local/share/bash-completion/completions/$$name; \
	done
	@echo "Bash completion installed. Open a new shell to activate."

# Alias for `make install` — kept for back-compat and discoverability now that
# `make install` includes the optional Qt GUI extra by default.
install-gui: install

# Copy the bundled Claude skills into the user's skills directory.
# Each is removed first so re-running overwrites any previous install,
# keeping the source-of-truth in this repo. `make install` depends on
# this target, so the skills install and update alongside the CLI.
install-skill:
	mkdir -p ~/.claude/skills
	@for skill in jailbee-repo-setup jailbee-usage; do \
		rm -rf ~/.claude/skills/$$skill; \
		cp -r docs/skills/$$skill ~/.claude/skills/; \
		echo "Installed: ~/.claude/skills/$$skill"; \
	done

# Run the full CI gate locally: lint, format check, type check, tests.
check:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/
	uv run mypy src/
	uv run pytest -q

# Build a clean sdist + wheel and validate the packaged metadata.
build:
	rm -rf dist/
	uv build
	uvx twine check dist/*

# Dry run: upload to TestPyPI (needs a TestPyPI token in ~/.pypirc or
# TWINE_* env vars). Use this before the first real release.
publish-testpypi: build
	uvx twine upload --repository testpypi dist/*

# Push the public lineage to the open-source repo.
#
#   make public-push [TAG=v1.0.1]
#
# This repo is the private one and keeps everything, including the pre-1.0
# history that was never published. The public repo's history starts at the
# 1.0.0 root commit and carries every commit from there on, unsquashed;
# pushes happen at release time rather than per commit, so the public repo
# lags this one in between. That is intentional, not a bug to fix.
#
# The whole point of this target is the checks. `git push --all`, `--tags`
# or `--mirror` to the public remote would republish the pre-1.0 history in
# one keystroke, so this pushes exactly one branch, and one tag by name.
#
# The structural check is the root-commit one: the public lineage must have
# exactly one root and it must be the commit tagged v1.0.0. Merging the
# archived pre-1.0 branch into main gives main a second root, which fails
# here rather than on the push.
PUBLIC_REMOTE ?= public
PUBLIC_BRANCH ?= main

public-push:
	@set -euo pipefail; \
	git remote get-url "$(PUBLIC_REMOTE)" >/dev/null 2>&1 \
	  || { echo "error: no '$(PUBLIC_REMOTE)' remote — see .local/RUNBOOK-export.md"; exit 1; }; \
	[ "$$(git rev-parse --abbrev-ref HEAD)" = "$(PUBLIC_BRANCH)" ] \
	  || { echo "error: public pushes are made from $(PUBLIC_BRANCH)"; exit 1; }; \
	git diff --quiet && git diff --cached --quiet \
	  || { echo "error: working tree is dirty — commit or stash first"; exit 1; }; \
	roots="$$(git rev-list --max-parents=0 HEAD)"; \
	if [ "$$(echo "$$roots" | wc -l)" -ne 1 ]; then \
	  echo "error: $(PUBLIC_BRANCH) has more than one root commit — the archived"; \
	  echo "       pre-1.0 history has been merged in and must not be published:"; \
	  echo "$$roots" | sed 's/^/         /'; exit 1; \
	fi; \
	if [ "$$roots" != "$$(git rev-parse v1.0.0^{commit})" ]; then \
	  echo "error: $(PUBLIC_BRANCH)'s root is $$roots, not the v1.0.0 root"; exit 1; \
	fi; \
	if [ -n "$(TAG)" ]; then \
	  git rev-parse -q --verify "refs/tags/$(TAG)" >/dev/null \
	    || { echo "error: no such tag: $(TAG)"; exit 1; }; \
	  git merge-base --is-ancestor "$(TAG)^{commit}" HEAD \
	    || { echo "error: $(TAG) is not on $(PUBLIC_BRANCH)"; exit 1; }; \
	fi; \
	echo "==> Pushing $(PUBLIC_BRANCH) to $(PUBLIC_REMOTE) ($$(git remote get-url $(PUBLIC_REMOTE)))"; \
	git push "$(PUBLIC_REMOTE)" "$(PUBLIC_BRANCH):$(PUBLIC_BRANCH)"; \
	if [ -n "$(TAG)" ]; then \
	  echo "==> Pushing tag $(TAG)"; \
	  git push "$(PUBLIC_REMOTE)" "refs/tags/$(TAG):refs/tags/$(TAG)"; \
	fi; \
	echo "==> Public refs now:"; \
	git ls-remote --heads --tags "$(PUBLIC_REMOTE)" | sed 's/^/    /'

# Draft/edit the Unreleased CHANGELOG section without cutting a release.
# Drafts entries from git history via the `claude` CLI (best effort), then
# opens the file for review. Nothing is committed.
changelog:
	-python3 scripts/changelog.py draft
	$${EDITOR:-vi} CHANGELOG.md

# Cut a release:  make release VERSION=x.y.z  [REGEN=1]
#
# Interactively finalizes the CHANGELOG (drafting from git history when the
# Unreleased section is empty, or always when REGEN=1), then bumps the
# version, commits, tags, builds, pushes, uploads to PyPI, and creates a
# GitHub Release with the built artifacts attached. PyPI upload needs a
# token (~/.pypirc or TWINE_* env vars); GitHub Release needs `gh` auth.
release:
	@set -euo pipefail; \
	v="$(VERSION)"; \
	[ -n "$$v" ] || { echo "Usage: make release VERSION=x.y.z [REGEN=1]"; exit 1; }; \
	echo "$$v" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+([.-]?[0-9A-Za-z]+)*$$' \
	  || { echo "error: VERSION '$$v' is not a valid version"; exit 1; }; \
	[ "$$(git rev-parse --abbrev-ref HEAD)" = "main" ] \
	  || { echo "error: releases are cut from main"; exit 1; }; \
	git diff --quiet && git diff --cached --quiet \
	  || { echo "error: working tree is dirty — commit or stash first"; exit 1; }; \
	if git rev-parse -q --verify "refs/tags/v$$v" >/dev/null; then \
	  echo "error: tag v$$v already exists"; exit 1; fi; \
	command -v gh >/dev/null || { echo "error: gh CLI not found"; exit 1; }; \
	command -v uv >/dev/null || { echo "error: uv not found"; exit 1; }; \
	echo "==> Running checks"; \
	$(MAKE) --no-print-directory check; \
	if python3 scripts/changelog.py unreleased-empty || [ -n "$(REGEN)" ]; then \
	  echo "==> Drafting CHANGELOG from git history"; \
	  python3 scripts/changelog.py draft || true; \
	fi; \
	echo "==> Review the CHANGELOG Unreleased section, then save & exit"; \
	$${EDITOR:-vi} CHANGELOG.md; \
	read -r -p "Proceed with release v$$v? [y/N] " ans; \
	case "$$ans" in y|Y) ;; *) echo "aborted"; exit 1;; esac; \
	echo "==> Bumping version to $$v"; \
	uv version "$$v"; \
	python3 scripts/changelog.py finalize "$$v"; \
	echo "==> Committing and tagging"; \
	git commit -aqm "chore: release $$v"; \
	git tag -a "v$$v" -m "Release $$v"; \
	echo "==> Building"; \
	$(MAKE) --no-print-directory build; \
	echo "==> Pushing commit and tag"; \
	git push origin main --follow-tags; \
	echo "==> Uploading to PyPI"; \
	uvx twine upload dist/*; \
	echo "==> Creating GitHub release"; \
	notes="$$(mktemp)"; \
	python3 scripts/changelog.py extract "$$v" > "$$notes"; \
	gh release create "v$$v" dist/* --title "v$$v" --notes-file "$$notes"; \
	rm -f "$$notes"; \
	echo "==> Released v$$v"
