.PHONY: install install-gui install-skill check build publish-testpypi \
        changelog release

# Use bash for recipe lines — the `release` target relies on bash features
# (`set -euo pipefail`, `read -r -p`) that /bin/sh does not provide.
SHELL := /bin/bash

# Install/reinstall the `jailbee` CLI globally from the current checkout WITH
# the optional Qt GUI extra (PySide6), then run the post-install steps:
# shell completions for `jailbee` and `jb`, the egress-refresh user timer,
# and the bundled Claude skills. `jailbee setup` is idempotent and `--yes`
# keeps it non-interactive (it never edits a shell rc in that mode), so
# re-running this target is safe. Requires libGL on the host; `jailbee gui`
# and `jailbee dashboard --gui` work after this.
install:
	uv tool install '.[gui]' --force --reinstall
	jailbee setup --yes

# Alias for `make install` — kept for back-compat and discoverability now that
# `make install` includes the optional Qt GUI extra by default.
install-gui: install

# Just the bundled Claude skills, for when only they changed. `make install`
# does this too, via `jailbee setup`, which is also what an end user runs —
# the copying lives in the package, not here, so there is one implementation
# to keep correct. Deliberately `uv run`, not the installed `jailbee`: from
# a checkout the skills to install are this tree's `docs/skills`, and the
# installed wheel carries the copy from whenever it was last built.
install-skill:
	uv run jailbee setup --yes --only skills

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

# Draft/edit the Unreleased CHANGELOG section without cutting a release.
# Drafts entries from git history via the `claude` CLI (best effort), then
# opens the file for review. Nothing is committed.
changelog:
	-python3 scripts/changelog.py draft
	$${EDITOR:-vi} CHANGELOG.md

# The repo `gh` acts on. Passed explicitly as `-R` rather than left to gh's
# own inference: gh needs a *default repository*, which it cannot derive when
# the checkout has several remotes or an SSH-alias URL it does not recognise
# as GitHub. That is how the 1.1.0 release failed — "No default remote
# repository has been set", raised by `gh release create`, which is the very
# last step, after the push and the PyPI upload had already happened.
REPO ?= VRTFinland/jailbee

# Cut a release:  make release VERSION=x.y.z  [REGEN=1]
#
# Interactively finalizes the CHANGELOG (drafting from git history when the
# Unreleased section is empty, or always when REGEN=1), then bumps the
# version, commits, tags, builds, pushes, uploads to PyPI, and creates a
# GitHub Release with the built artifacts attached. PyPI upload needs a
# token (~/.pypirc or TWINE_* env vars); GitHub Release needs `gh` auth with
# write access to $(REPO).
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
	gh repo view "$(REPO)" >/dev/null 2>&1 \
	  || { echo "error: gh cannot read $(REPO) — check \`gh auth status\`,"; \
	       echo "       or override the target with: make release VERSION=$$v REPO=owner/name"; \
	       exit 1; }; \
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
	python3 scripts/site_version.py set "$$v"; \
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
	gh release create "v$$v" dist/* --title "v$$v" --notes-file "$$notes" -R "$(REPO)"; \
	rm -f "$$notes"; \
	echo "==> Released v$$v"
