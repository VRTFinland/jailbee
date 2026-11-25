.PHONY: install install-gui install-web install-skill check build publish-testpypi \
        changelog release site site-check docs-serve web web-test web-e2e web-e2e-snapshots web-e2e-sweep web-corpus shots

# Use bash for recipe lines — the `release` target relies on bash features
# (`set -euo pipefail`, `read -r -p`) that /bin/sh does not provide.
SHELL := /bin/bash

# Install/reinstall the `jailbee` CLI globally from the current checkout WITH
# the optional Qt GUI extra (PySide6) and the optional SSH extra (asyncssh),
# then run the post-install steps: shell completions for `jailbee` and `jb`,
# the egress-refresh user timer, and the bundled agent skills when host-level
# `install_host_skills` is on (off by default). `jailbee setup` is idempotent
# and `--yes` keeps it non-interactive (it never edits a shell rc in that
# mode), so re-running this target is safe. Requires libGL on the host;
# `jailbee gui` and `jailbee dashboard --gui` work after this, and `jailbee
# remote ssh enable`/`serve` no longer need a separate `uv tool install`.
# The web dashboard's Python dependencies are core. Its browser app is
# gitignored and must be in the tree when the wheel is built, so it is built
# first whenever npm is available; without npm the install goes on and
# `jailbee serve` reports the missing app.
install:
	@if command -v npm >/dev/null 2>&1; then \
		$(MAKE) web; \
	else \
		echo "warning: npm not found; the web dashboard's browser app is not built (needs Node.js 22.22+ or 24.15+)" >&2; \
	fi
	uv tool install '.[gui,ssh]' --force --reinstall
	jailbee setup --yes
	@# The SSH, broker and web dashboard services run the old code until
	@# restarted (the broker restarts itself when the version changes, but a
	@# reinstall of the same version needs this). Only installed units are
	@# touched: try-restart is a no-op for a stopped unit but fails for a
	@# missing one.
	@if command -v systemctl >/dev/null 2>&1; then \
		for unit in jailbee-ssh jailbee-broker jailbee-web; do \
			if systemctl --user cat "$$unit.service" >/dev/null 2>&1; then \
				systemctl --user try-restart "$$unit.service"; \
			fi; \
		done; \
	fi
	@# The dashboards' state service too, and a reinstall of the same
	@# version never replaces it (the handshake compares versions only). Open
	@# dashboards reconnect and respawn it from the new code. The pattern is
	@# the service's whole argv tail (`spawn_server`), so a shell or editor that
	@# merely mentions it survives; `[_]` keeps it off this recipe's own shell.
	@pkill -u "$$(id -u)" -f -- ' -m jailbee [_]state-service$$' || true

# Alias for `make install` — kept for back-compat and discoverability now that
# `make install` includes the optional Qt GUI and SSH extras by default.
install-gui: install

# `make install` that requires the web dashboard's browser app: fails without
# npm instead of installing without it.
install-web:
	@command -v npm >/dev/null 2>&1 || { echo "error: npm not found; the web dashboard needs Node.js 22.22+ or 24.15+ and npm to build" >&2; exit 1; }
	$(MAKE) install

# Just the bundled agent skills, for when only they changed and host-level
# `install_host_skills` is on (off by default). `make install` does this too,
# via `jailbee setup`, which is also what an end user runs — the copying
# lives in the package, not here, so there is one implementation to keep
# correct. Deliberately `uv run`, not the installed `jailbee`: from
# a checkout the skills to install are this tree's `docs/skills`, and the
# installed wheel carries the copy from whenever it was last built.
install-skill:
	uv run jailbee setup --yes --only skills

# Build the web dashboard's browser app (frontend/) into the package's
# static directory, src/jailbee/web/static/, which `jailbee remote web serve`
# serves. That directory is gitignored: built assets are never committed.
# `npm ci` installs exactly what frontend/package-lock.json pins.
web:
	cd frontend && npm ci && npm run build

# The browser app's unit tests (Vitest).
web-test:
	cd frontend && npm ci && npm test

# The web dashboard's end-to-end browser suite (tests_e2e/, #152): builds the
# browser app as `make web` does, type-checks the suite, then drives the real
# server in the system Chrome. Its Python dependencies are the `e2e` group,
# which neither the ordinary suite nor the CI matrix installs. A failing
# test's Playwright trace and screenshot land in $(E2E_OUTPUT) (gitignored).
# Pass more pytest arguments with E2E_ARGS, e.g. `make web-e2e E2E_ARGS=-x`.
E2E_OUTPUT ?= _build/e2e-results
web-e2e:
	@command -v npm >/dev/null 2>&1 || { echo "error: npm not found; the e2e suite needs the browser app built (Node.js 22.22+ or 24.15+ and npm)" >&2; exit 1; }
	$(MAKE) web
	MYPYPATH=src uv run --group e2e mypy tests_e2e
	uv run --group e2e pytest tests_e2e -q --timeout 300 \
		--tracing retain-on-failure --screenshot only-on-failure \
		--output $(E2E_OUTPUT) $(E2E_ARGS)

# Accept intentional gallery changes. Only the gallery tests run, with no
# Playwright artifacts or pytest cache writes; review and commit the PNG diff.
web-e2e-snapshots:
	$(MAKE) web
	uv run --group e2e pytest tests_e2e/test_gallery_snapshots.py -q --timeout 300 \
		--update-gallery-snapshots -p no:cacheprovider

# Render every session and subagent of a Claude home in the web dashboard and
# report what broke (#152): `make web-e2e-sweep CLAUDE_HOME=<path>` (default
# ~/.claude). For the maintainer on the host, never CI. It starts the e2e test
# server with that home as its only repository's agent home and opens each
# transcript in the system Chrome. It only reads the home (the file watch is
# faked) and writes nothing but the report: Markdown at $(SWEEP_REPORT), under
# the gitignored .local/. The report starts with a summary line (also printed)
# and the unknown entry types across the home, then one section per session or subagent with a finding, headed
# `<project dir>/<session id>[/subagents/<agent id>]`, its dashboard URL and
# one bullet per finding: `load error` (an error page, or nothing rendered in
# 30 s), `JS error` (console or uncaught error), `CSP violation`, `slow`
# (navigation to rendered transcript, app start included, over SWEEP_SLOW_MS),
# `unknown entry type` (a record or content block type the reader neither
# renders nor knowingly skips, with count and first line) and `unparsable
# line`. Clean sessions are only counted.
CLAUDE_HOME ?= $(HOME)/.claude
SWEEP_SLOW_MS ?= 2000
SWEEP_REPORT ?= .local/web-e2e-sweep/$(shell date +%Y%m%d-%H%M%S).md
web-e2e-sweep:
	@command -v npm >/dev/null 2>&1 || { echo "error: npm not found; the sweep needs the browser app built (Node.js 22.22+ or 24.15+ and npm)" >&2; exit 1; }
	$(MAKE) web
	JAILBEE_SWEEP_HOME="$(CLAUDE_HOME)" JAILBEE_SWEEP_REPORT="$(abspath $(SWEEP_REPORT))" \
		JAILBEE_SWEEP_SLOW_MS="$(SWEEP_SLOW_MS)" \
		uv run --group e2e pytest tests_e2e/sweep_local.py -q -s --timeout 0 -p no:cacheprovider

# Re-record the web dashboard's corpus of genuine Claude Code sessions
# (tests/fixtures/transcripts/claude-corpus/). Needs a logged-in `claude`;
# it runs headless Claude (haiku) on a throwaway toy repository through the
# scripted prompts in scripts/web_corpus.py, so it spends a little quota. The
# output is normalised (stable ids, /work/toy paths, no host, account or
# credential) and refused if `find_leaks` still finds anything; the raw
# sessions are deleted from the Claude home afterwards. Review the diff before
# committing it, and re-record when Claude Code's transcript format changes.
web-corpus:
	uv run python -m scripts.web_corpus

# Run the full CI gate locally: lint, format check, type check, tests.
check:
	uv run ruff check src/ tests/ tests_e2e/
	uv run ruff format --check src/ tests/ tests_e2e/
	uv run mypy src/
	uv run pytest -n auto -q

# The documentation site generator, from the `docs` dependency group. Never a
# bare `zensical`: the pinned version is the one the config was written for.
ZENSICAL := uv run --only-group docs zensical

# Assemble the published site into _site/: the docs Zensical builds from the
# staged page whitelist, plus website/ exactly as committed. Order matters —
# the docs build cleans its own site_dir (_site/docs), so it runs first, and
# the landing page is copied over the top. `docs-theme/` is build input, not
# something to publish.
site:
	python3 scripts/docs_site.py stage
	$(ZENSICAL) build --clean --strict
	rsync -a --exclude '/docs-theme/' --exclude '/news/' website/ _site/
	uv run --only-group docs python -m scripts.news_site

# Enforce the site's no-CDN rule on the generated docs. Run after `make site`;
# CI runs both.
site-check:
	python3 scripts/docs_site.py check

# Preview the docs alone. The staged tree is a copy, so an edit to docs/ needs
# this target re-run; and the fonts and logo it borrows from the landing page
# only resolve in the assembled site (`make site`, then serve _site/).
docs-serve:
	python3 scripts/docs_site.py stage
	$(ZENSICAL) serve

# Build a clean sdist + wheel and validate the packaged metadata. `web` runs
# first: the built browser app is gitignored, so without it the wheel would
# ship a web server with no frontend.
build: web
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

# Cut a release:  make release VERSION=x.y.z  [REGEN=1] [SKIP_CHECK=1]
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
	[ -n "$$v" ] || { echo "Usage: make release VERSION=x.y.z [REGEN=1] [SKIP_CHECK=1]"; exit 1; }; \
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
	if [ -n "$(SKIP_CHECK)" ]; then \
	  echo "==> Skipping checks (SKIP_CHECK=1)"; \
	else \
	  echo "==> Running checks"; \
	  $(MAKE) --no-print-directory check; \
	fi; \
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

# Regenerate the dashboard screenshots (docs/images/dashboard/) from the seeded
# fixtures in website/shots/fixtures/: SVG for the docs, PNG (host Chrome) for
# videos. No Incus and no containers; see website/shots/README.md.
shots:
	uv run python -m website.shots
