"""Unit tests for resolve_snippets."""

import importlib.resources
from pathlib import Path

import pytest

from jailbee.golden import resolve_available, resolve_snippets


def _touch(path: Path, content: str = "#!/bin/bash\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def dirs(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    repo = tmp_path / "repo"
    return bundled, user, repo


def test_only_bundled_returned_when_user_and_repo_missing(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    _touch(bundled / "20-java.sh")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["10-locale.sh", "20-java.sh"]
    assert all(p.parent == bundled for p in result)


def test_user_shadow_wins_over_bundled(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh", "# bundled\n")
    _touch(user / "10-locale.sh", "# user\n")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["10-locale.sh"]
    assert result[0].read_text() == "# user\n"


def test_repo_shadow_wins_over_user_and_bundled(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh", "# bundled\n")
    _touch(user / "10-locale.sh", "# user\n")
    _touch(repo / "10-locale.sh", "# repo\n")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert result[0].read_text() == "# repo\n"


def test_user_added_name_appears_in_sorted_position(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    _touch(bundled / "20-java.sh")
    _touch(user / "15-extra.sh")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["10-locale.sh", "15-extra.sh", "20-java.sh"]


def test_repo_added_name_appears(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    _touch(repo / "55-postgres-libs.sh")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["10-locale.sh", "55-postgres-libs.sh"]


def test_disable_removes_by_filename_with_or_without_suffix(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    _touch(bundled / "70-claude.sh")
    _touch(bundled / "80-ecr-helper.sh")

    result = resolve_snippets(
        bundled_dir=bundled,
        user_dir=user,
        repo_dir=repo,
        disabled=["70-claude", "80-ecr-helper.sh"],
    )

    assert [p.name for p in result] == ["10-locale.sh"]


def test_disable_removes_by_logical_name(dirs):
    """`disable_snippets` accepts a logical name (no numeric prefix), matching
    how `enable_snippets` resolves names — so `["registry-mirror-ca"]` drops
    `90-registry-mirror-ca.sh`."""
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    _touch(bundled / "90-registry-mirror-ca.sh")

    result = resolve_snippets(
        bundled_dir=bundled,
        user_dir=user,
        repo_dir=repo,
        disabled=["registry-mirror-ca"],
    )

    assert [p.name for p in result] == ["10-locale.sh"]


def test_empty_file_preserved_in_result(dirs):
    """An empty repo-level shadow file is preserved in the result. Runtime
    skip happens inside install.sh's `[ -s "$f" ]` check, not here.
    """
    bundled, user, repo = dirs
    _touch(bundled / "70-claude.sh", "# real claude install\n")
    (repo / "70-claude.sh").parent.mkdir(parents=True, exist_ok=True)
    (repo / "70-claude.sh").write_text("")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["70-claude.sh"]
    assert result[0].stat().st_size == 0


def test_missing_dirs_dont_crash(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    # user and repo directories never created

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["10-locale.sh"]


def test_only_files_with_sh_suffix_considered(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    _touch(bundled / "README.md", "not a snippet")

    result = resolve_snippets(bundled_dir=bundled, user_dir=user, repo_dir=repo, disabled=[])

    assert [p.name for p in result] == ["10-locale.sh"]


# --- Phase E Task 2: available-library staging via enable_snippets ----------


def test_enable_by_friendly_name_stages_from_available(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    avail = bundled.parent / "available"
    _touch(avail / "30-nodejs.sh")
    _touch(avail / "50-docker.sh")
    result = resolve_snippets(
        bundled_dir=bundled,
        user_dir=user,
        repo_dir=repo,
        disabled=[],
        available_dir=avail,
        enabled=["nodejs"],
    )
    assert [p.name for p in result] == ["10-locale.sh", "30-nodejs.sh"]


def test_enable_by_full_filename_also_works(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    avail = bundled.parent / "available"
    _touch(avail / "30-nodejs.sh")
    result = resolve_snippets(
        bundled_dir=bundled,
        user_dir=user,
        repo_dir=repo,
        disabled=[],
        available_dir=avail,
        enabled=["30-nodejs"],
    )
    assert [p.name for p in result] == ["10-locale.sh", "30-nodejs.sh"]


def test_not_enabled_available_snippet_is_absent(dirs):
    bundled, user, repo = dirs
    _touch(bundled / "10-locale.sh")
    avail = bundled.parent / "available"
    _touch(avail / "50-docker.sh")
    result = resolve_snippets(
        bundled_dir=bundled,
        user_dir=user,
        repo_dir=repo,
        disabled=[],
        available_dir=avail,
        enabled=[],
    )
    assert [p.name for p in result] == ["10-locale.sh"]


def test_disable_beats_enable(dirs):
    bundled, user, repo = dirs
    avail = bundled.parent / "available"
    _touch(avail / "50-docker.sh")
    result = resolve_snippets(
        bundled_dir=bundled,
        user_dir=user,
        repo_dir=repo,
        disabled=["50-docker"],
        available_dir=avail,
        enabled=["docker"],
    )
    assert result == []


def test_resolve_available_reports_unknown(tmp_path):
    avail = tmp_path / "available"
    _touch(avail / "30-nodejs.sh")
    matched, unknown = resolve_available(avail, ["nodejs", "bogus"])
    assert [p.name for p in matched] == ["30-nodejs.sh"]
    assert unknown == ["bogus"]


# --- Phase B Task 4: bundled split regression assertions ---------------------


def test_bundled_install_d_contains_expected_snippets():
    """The bundled install.d/ shipped in the wheel must contain the
    feature snippets enumerated in the spec / plan.
    """
    bundled = importlib.resources.files("jailbee.provision").joinpath("install.d")
    names = sorted(p.name for p in bundled.iterdir() if p.name.endswith(".sh"))

    expected = [
        "05-extra-apt.sh",
        "10-locale.sh",
        "15-prompt.sh",
        "60-gui-libs.sh",
        "75-github-cli.sh",
    ]
    assert names == expected


def test_openjdk_snippet_is_in_available_library():
    available = Path(
        str(importlib.resources.files("jailbee.provision").joinpath("install.d.available"))
    )
    matched, unknown = resolve_available(available, ["20-openjdk"])
    assert not unknown
    assert [p.name for p in matched] == ["20-openjdk.sh"]
    body = matched[0].read_text()
    assert "JAVA_PACKAGE" in body
    assert "apt.corretto.aws" not in body  # no external repo — Ubuntu archive only


def test_all_stack_snippet_names_resolve_in_available_library():
    """Every snippet name Stacks.snippet_names() can emit must exist in the
    bundled install.d.available/ library — otherwise enabling that stack would
    silently stage nothing (resolve_available drops unmatched tokens)."""
    from jailbee.config import Stacks

    available = Path(
        str(importlib.resources.files("jailbee.provision").joinpath("install.d.available"))
    )
    full = Stacks(java="corretto-17", node=22, python=True, docker=True, ecr=True)
    names = set(full.snippet_names()) | set(Stacks(java="openjdk-21").snippet_names())
    _matched, unknown = resolve_available(available, sorted(names))
    assert not unknown, f"stack snippet names with no install.d.available/ file: {unknown}"


def test_available_library_contains_optin_snippets():
    avail = importlib.resources.files("jailbee.provision").joinpath("install.d.available")
    names = sorted(p.name for p in avail.iterdir() if p.name.endswith(".sh"))
    assert names == [
        "20-corretto.sh",
        "20-openjdk.sh",
        "30-nodejs.sh",
        "40-python.sh",
        "50-docker.sh",
        "80-ecr-helper.sh",
        "90-registry-mirror-ca.sh",
    ]


def test_github_cli_script_uses_cli_github_com_keyring():
    """The gh installer must pull from GitHub's own apt repo, not Ubuntu
    universe — so the container always gets the latest stable gh.
    """
    script = (
        importlib.resources.files("jailbee.provision")
        .joinpath("install.d/75-github-cli.sh")
        .read_text()
    )
    assert script.startswith("#!/bin/bash")
    assert "set -euo pipefail" in script
    assert "cli.github.com/packages/githubcli-archive-keyring.gpg" in script
    assert "DEBIAN_FRONTEND=noninteractive apt-get install -y gh" in script


def test_slim_install_sh_does_not_install_toolchain():
    """Smoke-style content check: after the split, install.sh main must
    not contain references to Corretto, NodeSource, Claude, etc. — those
    live in their snippets.
    """
    install_sh = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()

    forbidden_in_main = [
        "apt.corretto.aws",  # 20-corretto.sh
        "deb.nodesource.com",  # 30-nodejs.sh
        "claude.ai/install.sh",  # ensure-claude.sh (runs at gie-new time, not golden-build)
        "amazon-ecr-credential-helper",  # 80-ecr-helper.sh
        "download.docker.com",  # 50-docker.sh
        "jailbee-registry-mirror",  # 90-registry-mirror-ca.sh
    ]
    for needle in forbidden_in_main:
        assert needle not in install_sh, (
            f"{needle!r} should have moved out of install.sh main into a snippet"
        )


def test_slim_install_sh_has_snippet_loop():
    install_sh = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    assert "/provision/install.d/" in install_sh
    assert '[ -s "$f" ]' in install_sh


def test_install_sh_precreates_bind_mount_parents_as_dev_user():
    """The bind-mount parent precreation must run as ${CONTAINER_USER},
    not as root. The previous "mkdir -p as root + chown only the leaf"
    pattern left intermediate dirs (e.g. /home/dev/.local,
    /home/dev/.local/share, /home/dev/.java) owned by root:root, which
    blocked installers running as dev from writing siblings — notably
    the Claude Code native installer failed EACCES on
    `mkdir /home/dev/.local/share/claude` (#).
    Claude no longer installs at golden-build time, but other snippets
    still need dev-owned ~/.local/* — the precreation must stay.
    """
    install_sh = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    assert 'runuser -u "${CONTAINER_USER}" -- mkdir -p "${JAILBEE_USER_HOME}/${d}"' in install_sh, (
        "bind-mount parent precreation must mkdir as the dev user so every "
        "intermediate dir is dev-owned (not just the leaf via chown)"
    )


def test_install_sh_precreates_dot_cache_as_dev_user():
    """`.cache` must be in the precreated dev-owned parent list. A repo can
    bind-mount a shared cache under it (e.g. `~/.cache/uv`), which makes
    Incus auto-create `.cache` as root:root. The Claude Code native
    installer then fails EACCES on `mkdir ~/.cache/claude`, leaving an
    empty version store and no `claude` binary.
    """
    install_sh = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    precreate_block = install_sh.split("Pre-creating bind-mount parent dirs", 1)[1]
    for_body = precreate_block.split("for d in", 1)[1].split("; do", 1)[0]
    assert "\n    .cache \\" in for_body, (
        "`.cache` must be precreated dev-owned so `~/.cache/<shared-cache>` "
        "mounts don't leave `.cache` root:root and break the Claude installer"
    )


def test_install_sh_exports_container_user_for_snippets():
    """Snippets execute as `bash "$f"` (a fresh process) and several of
    them (15-prompt.sh, 30-nodejs.sh, 50-docker.sh) reference
    $CONTAINER_USER under `set -u`. Since CONTAINER_USER is intentionally
    NOT passed via `incus exec --env` (see test_golden.py), it
    must be exported from install.sh itself so subprocesses inherit it.
    """
    install_sh = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    assert "export CONTAINER_USER=" in install_sh, (
        "install.sh must `export CONTAINER_USER=...` so install.d/* subprocesses see it"
    )
