# Releasing

`jailbee` is released to [PyPI](https://pypi.org/) and, in the same step, to the
[GitHub Releases](https://github.com/VRTFinland/jailbee/releases) page. The whole
release flow runs locally from the `Makefile` — no CI pipeline publishes
(the repo's CI workflow only runs lint, type-check, and tests).

The version lives in exactly one place: `version` in `pyproject.toml`.
`jailbee.__version__` reads it back from the installed package
metadata, so a release only ever bumps that one field.

## Prerequisites (one-time)

1. **PyPI account** with the project name available. Check it first:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/jailbee/json
   # 404 = the name is free
   ```
2. **PyPI API token.** Create one at <https://pypi.org/manage/account/token/>
   and store it either in `~/.pypirc`:
   ```ini
   [pypi]
   username = __token__
   password = pypi-AgEIcHl…            # your token

   [testpypi]
   username = __token__
   password = pypi-AgEIcHl…            # a separate TestPyPI token
   ```
   or in the environment for a single run:
   ```bash
   export TWINE_USERNAME=__token__
   export TWINE_PASSWORD=pypi-AgEIcHl…
   ```
3. **GitHub CLI authenticated** (`gh auth status` should be green) — used to
   create the GitHub Release and upload the built artifacts. The target names
   the repo explicitly (`REPO ?= VRTFinland/jailbee`) and verifies `gh` can
   read it during preflight, so no `gh repo set-default` is needed. Override
   with `make release VERSION=… REPO=owner/name` if you ever release a fork.
4. **A clean checkout of `main`.** The release target refuses to run from any
   other branch or with a dirty working tree.

## Claiming the name (one-time, before the first release)

A PyPI `(name, version)` pair is permanent — a version can be deleted, but the
same one can never be uploaded again. So don't spend `1.0.0` on claiming the
name: publish a pre-release. The tree carries `1.0.0rc1` for exactly this, and
neither `pip` nor `uv` resolves a pre-release by default, so nobody installs it
by accident while the project is still being finished.

`make release` is the wrong tool here — it insists on `main`, pushes a tag and
creates a GitHub Release, none of which claiming a name needs (and the public
repo may not exist yet). Upload the built artifacts directly:

```bash
make build                # sdist + wheel, validated with twine check
uvx twine upload dist/*
```

Two things that catch people out:

- **TestPyPI is a separate namespace.** `make publish-testpypi` proves the
  metadata renders and the token works, but it does not claim the name on
  PyPI. Claim it on both.
- **The first upload of a new project needs an account-scoped token.** A
  project-scoped token cannot be created before the project exists. Create a
  project-scoped one and delete the wide one immediately afterwards.

The real release is cut later with `make release VERSION=1.0.0`, which runs
`uv version` itself — the rc in the tree needs no manual undoing.

## Dry run (recommended before the first real release)

Upload to [TestPyPI](https://test.pypi.org/) to confirm the package builds,
the metadata validates, and the token works:

```bash
make publish-testpypi
```

Then check the rendered project page and, optionally, install from TestPyPI in
a throwaway environment.

## Cutting a release

```bash
make release VERSION=1.0.1
```

This is **interactive** and stops for review before anything irreversible
happens. Step by step:

1. **Preflight** — verifies you are on `main`, the tree is clean, `VERSION`
   is a valid version, the `v1.0.1` tag does not already exist, and `gh`/`uv`
   are installed. Then runs `make check` (ruff, format check, mypy, pytest).
2. **CHANGELOG** — if the `## Unreleased` section is empty (or you pass
   `REGEN=1`), entries are drafted from `git log` since the last tag using the
   `claude` CLI. The `CHANGELOG.md` then opens in `$EDITOR` for you to review
   and edit. Save and exit when it reads the way you want.
3. **Confirmation** — you are asked `Proceed with release v1.0.1? [y/N]`.
   Answering anything but `y`/`Y` aborts with no changes pushed or published.
4. **Publish** — only after confirmation: bump the version (`uv version`),
   finalize the CHANGELOG (`## Unreleased` → `## 1.0.1 - <date>`, plus a fresh
   empty `## Unreleased`), commit, tag `v1.0.1`, build, push the commit and
   tag, `twine upload` to PyPI, and `gh release create` with the sdist + wheel
   attached and the release notes taken from the CHANGELOG section.

The release commit is minimal: `pyproject.toml`, `uv.lock`, and `CHANGELOG.md`.

### Flags

- `REGEN=1` — re-draft the CHANGELOG from git history even if the Unreleased
  section already has content:
  ```bash
  make release VERSION=1.0.1 REGEN=1
  ```

## Drafting the CHANGELOG without releasing

To draft and edit the Unreleased section on its own — e.g. while wrapping up a
batch of work — without cutting a release:

```bash
make changelog
```

This drafts from git history (best effort; skipped if `claude` is unavailable
or there are no new commits) and opens `CHANGELOG.md` in `$EDITOR`. Nothing is
committed.

## If something goes wrong

The flow does all local work — bump, finalize, commit, tag, build — before it
touches anything remote, so a failure in the early steps leaves the remote
untouched. If a step fails **after** the push but before PyPI/GitHub finished:

- **`twine upload` failed** — fix the cause (e.g. token) and re-run
  `uvx twine upload dist/*`. PyPI rejects re-uploading an existing version, so
  a partial upload of the same version cannot be clobbered; bump to a new
  version only if the artifacts themselves were wrong.
- **`gh release create` failed** — re-run it manually. `make release` cannot be
  re-run at this point: the tag now exists, so its preflight refuses.
  ```bash
  python3 scripts/changelog.py extract 1.0.1 > notes.md
  gh release create v1.0.1 dist/* --title v1.0.1 --notes-file notes.md \
    -R VRTFinland/jailbee
  ```
  `-R` is what makes this work regardless of how the checkout's remotes are
  set up. This is how the 1.1.0 release failed: `gh` reported "No default
  remote repository has been set", which it does when a checkout has several
  remotes or an SSH-alias URL it cannot recognise as GitHub. The target now
  passes `-R $(REPO)` itself and checks the repo is readable in preflight, so
  the same cause fails *before* the push rather than after the PyPI upload.

To undo a release that was only committed/tagged locally (nothing pushed):

```bash
git tag -d v1.0.1
git reset --hard HEAD~1
```

## Helper script

`scripts/changelog.py` (pure stdlib) does the deterministic CHANGELOG surgery
used by the targets above:

| Command | Purpose |
|---------|---------|
| `finalize <version> [--date YYYY-MM-DD]` | stamp `## Unreleased` with a version + date and open a fresh Unreleased |
| `extract <version>` | print a version's section body (used as GitHub Release notes) |
| `unreleased-empty` | exit 0 if the Unreleased section is empty, 1 otherwise |
| `draft [--from <ref>]` | draft Unreleased entries from `git log` via the `claude` CLI |
