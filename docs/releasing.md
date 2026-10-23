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

## Reviewing dependencies before a release

Do this while preparing the CHANGELOG — before `make release`, on the branch
that will be merged to `main` — so any bump lands in the release it is
reviewed for and gets its own CHANGELOG entry and, where needed, an
`UPGRADE_NOTES` entry.

### Dependabot pull requests

Dependabot opens weekly PRs against `uv.lock` (and GitHub Actions). List the
open ones:

```bash
gh pr list -R VRTFinland/jailbee --author 'app/dependabot' --state open
```

For each:

- **Already superseded?** Compare the PR's target version with `uv.lock` on
  the release branch. A later bump (another PR, or a manual `uv lock`) leaves
  the PR stale; close it on GitHub.
- **Read the upstream changelog**, at least for the range being skipped.
  Major versions (mypy 1 → 2 and the like) usually need code changes; leave
  those for their own commit rather than folding them into the release.
- **Check CI** (`gh pr checks <n>`). A red PR is not merged as is.
- **Merge** the ones that pass. From inside a container, where `gh` is
  read-only, apply the same bump locally and commit it — Dependabot closes
  its PR once the change reaches the default branch:
  ```bash
  uv lock --upgrade-package <name>==<version>
  ```
  Edit `pyproject.toml` first when the dependency is pinned there with `==`.
  Run `make check` (and `make site site-check` for the docs toolchain).

These are development and build dependencies; a runtime dependency bump of
`jailbee` itself goes in the CHANGELOG only if a user would notice.

### The LiteLLM proxy pin

`PINNED_LITELLM_VERSION` in `src/jailbee/config/models_litellm.py` and the
hash-locked `src/jailbee/provision/litellm/requirements.lock` decide what
`jailbee litellm up` installs into a container that holds provider
credentials. LiteLLM has been compromised on PyPI twice (1.82.7/1.82.8 in
March 2026, 1.107.0 on 2026-10-21 — both credential stealers that ran at
Python startup), so this pin is updated **conservatively and by hand**:

1. **Check whether there is a reason to move.** List the releases since the
   pin (`https://pypi.org/pypi/litellm/json`, or the GitHub releases page)
   and read their notes. Prefer patch releases on the pinned stable line
   (`1.106.x`) over a new minor. No security fix and no bug that affects
   jailbee's routes → stay where you are.
2. **Let a candidate age.** Pin only a stable release (no `rc`/`dev`) that has
   been on PyPI for at least **7 days**, and search for security advisories
   or incident reports about it and its neighbours before trusting it.
   Compromised uploads have so far been found and pulled within hours to
   days.
3. **Verify the artifacts.** Download the wheel(s) and sdist into an empty
   scratch directory — never `pip install` them — and check:
   - the sha256 of each file equals the digest PyPI publishes for it;
   - there is no `.pth` file and no new top-level module outside the
     package, and `entry_points.txt` and `Requires-Dist` changed only as the
     release notes explain;
   - the Python sources are byte-identical to the GitHub release tag
     (`diff -r` of the extracted wheel against the tag's tarball); the same
     for `litellm-enterprise` and any other `litellm-*` package whose version
     moves;
   - skim the diff from the current pin (`gh api
     repos/BerriAI/litellm/compare/v<old>...v<new>`), at least for anything
     touching packaging, startup hooks, network calls or credential handling.
   A native extension (`rust_bridge/_native.abi3.so`) cannot be checked
   against source this way; note that in the commit message.
4. **Regenerate the lock moving only LiteLLM's own packages.** Keep every
   other pin as it is, and cut the index off at a date you have reviewed:
   ```bash
   cp src/jailbee/provision/litellm/requirements.lock new.lock
   echo 'litellm[proxy]==<version>' > litellm-req.in
   uv pip compile litellm-req.in --universal --generate-hashes \
     --python-version 3.12 --prerelease=if-necessary-or-explicit -o new.lock \
     --upgrade-package litellm --upgrade-package litellm-enterprise \
     --upgrade-package litellm-proxy-extras \
     --exclude-newer <ISO timestamp just after the candidate's upload>
   ```
   Run this in a scratch directory and copy `new.lock` back. Diff the
   package lines: anything beyond the `litellm-*` packages moving is a
   transitive change the new release requires — review each one like the
   candidate itself, or do not take the release.
5. **Update the pin everywhere** — `PINNED_LITELLM_VERSION`, the version in
   `docs/litellm.md` and `docs/config.md`, the test fixtures that carry it —
   and add a `### Changed` CHANGELOG entry plus an `UPGRADE_NOTES` entry
   (`apply`: `jailbee apply` reports that the proxy needs
   `jailbee litellm up`). The commit message records what was verified and
   what could not be.

The proxy's egress lockdown (package hosts only, without the state volume,
while installing; provider hosts only afterwards) limits what a malicious
release could reach, but it does not make one harmless: the proxy holds
provider credentials, can spend them on the allowed providers, and resolves
names through the bridge's DNS.

A user who sets `litellm.version` installs without hashes and takes
whatever PyPI serves for that version; that is documented as their choice,
and nothing here protects it.

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
   `REGEN=1`), entries are drafted from `git log` since the last release tag
   using the `claude` CLI. The `CHANGELOG.md` then opens in `$EDITOR` for you
   to review and edit — see [What belongs in the Unreleased
   section](#what-belongs-in-the-unreleased-section) for what to keep and what
   to cut. While you're in there: check whether anything in this release
   changed what `jb base build` produces or what `jb apply` writes; if so,
   `UPGRADE_NOTES` in `src/jailbee/upgrade.py` needs an entry at **this**
   release's version, with a one-line reason a user can act on. The entry is
   invisible until that version ships and silently wrong if the version
   number changes afterward, so confirm it matches `VERSION` above. Also
   confirm the [dependency review](#reviewing-dependencies-before-a-release)
   was done for this release: open Dependabot PRs handled, and the LiteLLM
   pin either deliberately kept or bumped through the verified procedure.
   Save and exit when it reads the way you want.
3. **Confirmation** — you are asked `Proceed with release v1.0.1? [y/N]`.
   Answering anything but `y`/`Y` aborts with no changes pushed or published.
4. **Publish** — only after confirmation: bump the version (`uv version`),
   finalize the CHANGELOG (`## Unreleased` → `## 1.0.1 - <date>`, plus a fresh
   empty `## Unreleased`), commit, tag `v1.0.1`, build, push the commit and
   tag, `twine upload` to PyPI, and `gh release create` with the sdist + wheel
   attached and the release notes taken from the CHANGELOG section.

The release commit is minimal: `pyproject.toml`, `uv.lock`, `CHANGELOG.md`, and
`website/index.html` — the last because the site is served as committed, so the
version its JSON-LD advertises is a literal that has to be rewritten with the
rest.

### Flags

- `REGEN=1` — re-draft the CHANGELOG from git history even if the Unreleased
  section already has content:
  ```bash
  make release VERSION=1.0.1 REGEN=1
  ```

## What belongs in the Unreleased section

The changelog is read by someone upgrading from one released version to
another. The released versions are exactly the `v*` tags — everything after
the newest one is unreleased, and nobody has ever run any intermediate state
of it. So the `## Unreleased` section describes the **net difference from the
last tag**, not the route the work took to get there:

```bash
git log $(git describe --tags --abbrev=0 --match 'v[0-9]*')..HEAD --oneline
```

- **One feature, one entry.** A feature built over ten commits is written as
  if it had arrived finished.
- **A fix to something unreleased gets no entry of its own.** If the bug was
  introduced in this same range, no user could hit it, so it is not news. Fold
  its outcome into the entry for the feature it belongs to — usually silently,
  because that entry should describe the behaviour as it now ships. It never
  goes under `### Fixed`.
- **`### Fixed` means "broken in a version you can have installed."** The
  regression has to predate the last tag. A fix for a feature that shipped in
  the *previous* release is a `### Fixed` entry; a fix for a feature that is
  still sitting in Unreleased is not.
- **Added-then-reverted work is omitted entirely.** The net difference is
  nothing.
- **Internal churn gets no entry** — tests, refactors, lint, CI, plumbing —
  unless it changes what a user sees or does.

The test for any entry: does it tell a user something they can act on? An
entry that only makes sense to someone who watched the branch develop is
churn, and cutting it makes the section easier to read, not poorer.

The auto-draft (`scripts/changelog.py draft`) is told all of this, but it
works from commit subjects and cannot know which of them cancel out — the
review in `$EDITOR` is where the folding actually happens. Trimming is an edit
to the *upcoming* section only: released sections are historical records and
are never rewritten, even where they carry entries this rule would have cut.

## Drafting the CHANGELOG without releasing

To draft and edit the Unreleased section on its own — e.g. while wrapping up a
batch of work — without cutting a release:

```bash
make changelog
```

This drafts from git history (best effort; skipped if `claude` is unavailable
or there are no new commits) and opens `CHANGELOG.md` in `$EDITOR`. Nothing is
committed. The same rule applies as at release time — see [What belongs in the
Unreleased section](#what-belongs-in-the-unreleased-section); a draft written
mid-batch is the most likely place for an entry about a fix to something that
has not shipped.

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

## Helper scripts

Both are pure stdlib and do the deterministic file surgery the targets above
rely on.

`scripts/changelog.py`:

| Command | Purpose |
|---------|---------|
| `finalize <version> [--date YYYY-MM-DD]` | stamp `## Unreleased` with a version + date and open a fresh Unreleased |
| `extract <version> [--raw]` | print a version's section body (used as GitHub Release notes), with each block's hard-wrapped lines joined onto one line; `--raw` prints it verbatim |
| `unreleased-empty` | exit 0 if the Unreleased section is empty, 1 otherwise |
| `draft [--from <ref>]` | draft Unreleased entries from `git log` since the last `v*` tag via the `claude` CLI |

`scripts/site_version.py`:

| Command | Purpose |
|---------|---------|
| `get` | print the version each site literal advertises: `website/index.html`'s JSON-LD and header source box, and `[project.extra] version` in `zensical.toml` (the docs header's source box) |
| `set <version>` | rewrite all three (exits non-zero, writing nothing, unless each has exactly one match) |

`set` runs during a release, after `uv version` and before the commit, so a
stale value aborts the release while everything is still local. Without it the
drift only surfaced afterwards, as
`test_the_structured_data_version_tracks_pyproject` going red on `main` — which
is how it was found.
