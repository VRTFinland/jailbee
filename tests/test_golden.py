"""Tests for golden image building."""

import base64
import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.config import Stacks, load_config
from jailbee.golden import (
    ArchivedImage,
    GoldenImageUsage,
    build_golden_image,
    find_all_archived_images,
    find_archived_images,
    gather_golden_usage,
    repo_uses_docker,
    resolved_snippet_names,
    resolved_snippet_paths,
)
from jailbee.incus import IncusError

FIXTURES = Path(__file__).parent / "fixtures"


def test_build_calls_launch_exec_publish_delete():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False

    build_golden_image(cfg, incus)

    expected_build_name = f"{cfg.container_prefix}-base-build"

    assert incus.launch.called
    launch_args = incus.launch.call_args.args
    launch_kwargs = incus.launch.call_args.kwargs
    # incus.launch(image, name, config=..., network=...)
    assert "ubuntu" in launch_args[0].lower()
    assert launch_args[1] == expected_build_name
    # Nesting required so systemd-networkd starts inside the build container
    assert launch_kwargs.get("config", {}).get("security.nesting") == "true"
    # Build must use jailbee-loose to bypass the strict ACL on incusbr0.
    assert launch_kwargs.get("network") == "jailbee-loose"

    # exec is called to copy provision script and run it
    assert incus.exec.called
    incus.publish.assert_called_once()
    incus.delete.assert_called_once_with(expected_build_name, force=True)


def test_build_container_name_uses_prefix(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)
    launched_name = incus.launch.call_args.args[1]
    assert launched_name == f"{cfg.container_prefix}-base-build"


def test_build_uses_config_versions():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)

    # exec should pass the configured Java/Node versions in env
    env_calls = [c.kwargs.get("env", {}) for c in incus.exec.call_args_list]
    merged: dict[str, str] = {}
    for envd in env_calls:
        if envd:
            merged.update(envd)
    # Java package should mention corretto when configured
    assert any("corretto" in str(v).lower() for v in merged.values())
    # Node major version
    assert merged.get("NODE_MAJOR") == "24"
    # Python is no longer a configurable version — it comes from the base
    # image, so no PYTHON_VERSION env is passed to the provisioning script.
    assert "PYTHON_VERSION" not in merged


def test_build_warns_on_deprecated_pinned_python(mocker):
    """full_config.yaml still pins `golden.python`; the build must ignore it
    (no PYTHON_VERSION env) and emit a soft deprecation warning."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    assert cfg.golden.python  # fixture pins a version → triggers the warning
    warn = mocker.patch("jailbee.golden.warn")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False

    build_golden_image(cfg, incus)

    assert warn.called
    assert any("golden.python" in str(c.args[0]) for c in warn.call_args_list)


def test_build_archives_existing_alias_before_publish():
    """Rebuilding crashes with 'Aliases already exists' because
    publish doesn't displace the existing alias. Archive it as
    `<alias>-<YYYY-MM-DD>` first.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = True

    build_golden_image(cfg, incus)

    incus.image_alias_rename.assert_called_once()
    old, archived = incus.image_alias_rename.call_args.args
    assert old == cfg.golden.alias
    # archived alias has the form `<alias>-YYYY-MM-DD`
    assert archived.startswith(f"{cfg.golden.alias}-")
    assert len(archived.removeprefix(f"{cfg.golden.alias}-")) == 10  # YYYY-MM-DD

    # Rename must happen before publish, so the new image can claim the
    # primary alias without colliding.
    timeline = incus.mock_calls
    rename_idx = next(i for i, c in enumerate(timeline) if c[0] == "image_alias_rename")
    publish_idx = next(i for i, c in enumerate(timeline) if c[0] == "publish")
    assert rename_idx < publish_idx


def test_build_skips_archive_when_no_existing_alias():
    """First-time build: image_exists returns False → no rename call."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False

    build_golden_image(cfg, incus)

    incus.image_alias_rename.assert_not_called()
    incus.publish.assert_called_once()


def test_build_replaces_same_day_archive_alias():
    """Rebuilding on the same day: the prior archive image
    `<alias>-YYYY-MM-DD` already exists. Delete it (not just its alias) before
    renaming so the rename doesn't fail with 'Alias already exists' and no
    aliasless image is left dangling.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    # First call (alias check): True (current alias exists).
    # Second call (archive check): True (same-day archive exists too).
    incus.image_exists.side_effect = [True, True]

    build_golden_image(cfg, incus)

    # Existing archive image is deleted first.
    incus.image_delete.assert_called_once()
    deleted = incus.image_delete.call_args.args[0]
    assert deleted.startswith(f"{cfg.golden.alias}-")

    # Then the current alias is renamed into the freed name.
    incus.image_alias_rename.assert_called_once()
    old, new = incus.image_alias_rename.call_args.args
    assert old == cfg.golden.alias
    assert new == deleted

    # Order: delete archive → rename → publish.
    timeline = [c[0] for c in incus.mock_calls]
    del_idx = timeline.index("image_delete")
    rename_idx = timeline.index("image_alias_rename")
    publish_idx = timeline.index("publish")
    assert del_idx < rename_idx < publish_idx


def test_build_does_not_delete_archive_when_archive_alias_missing():
    """Day-N rebuild where today's archive slot is empty: archive the
    current alias straight into it without any deletion."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    # alias check: True; archive check: False.
    incus.image_exists.side_effect = [True, False]

    build_golden_image(cfg, incus)

    incus.image_alias_delete.assert_not_called()
    incus.image_alias_rename.assert_called_once()


def test_build_same_day_deletes_image_not_just_alias(make_cfg, tmp_path):
    """Same-day rebuild: delete the whole archived image (not just its
    alias) so no aliasless image is left dangling."""
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = True  # both live alias and archive exist → same-day
    build_golden_image(cfg, incus)
    archived_arg = incus.image_delete.call_args.args[0]
    assert archived_arg.startswith(f"{cfg.golden.alias}-")
    incus.image_alias_delete.assert_not_called()


def test_build_same_day_falls_back_to_alias_delete_when_in_use(make_cfg, tmp_path):
    """Same-day rebuild: if the archive is still in use by a container,
    Incus refuses the delete — fall back to dropping just the alias so the
    rename can proceed and the build does not fail."""
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = True
    incus.image_delete.side_effect = IncusError("Image is currently in use")
    build_golden_image(cfg, incus)
    # image_delete attempted, then fell back to alias delete so rename can proceed
    incus.image_delete.assert_called_once()
    incus.image_alias_delete.assert_called_once()
    incus.image_alias_rename.assert_called_once()


# ---------- provision_script + provision_env


def _find_install_run_call(incus):
    return next(
        c for c in incus.exec.call_args_list if c.args[1] == ["bash", "/provision/install.sh"]
    )


def _find_install_copy_call(incus):
    """Locate the base64 copy of install.sh into the build container."""
    for c in incus.exec.call_args_list:
        cmd = c.args[1]
        if cmd[:2] == ["bash", "-c"] and "base64 -d > /provision/install.sh" in cmd[2]:
            return c
    raise AssertionError("install.sh copy exec not found")


def test_build_uses_custom_provision_script(make_cfg, tmp_path, mocker):
    script = tmp_path / "custom.sh"
    script.write_text("#!/bin/bash\necho custom-marker-xyz\n")
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"provision_script": script},
            ),
        }
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)

    copy_call = _find_install_copy_call(incus)
    match = re.search(r"echo '([^']+)'", copy_call.args[1][2])
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode()
    assert "custom-marker-xyz" in decoded


def test_build_uses_relative_provision_script_under_repo_root(
    make_cfg,
    tmp_path,
    mocker,
):
    (tmp_path / ".gie").mkdir()
    rel_script = tmp_path / ".gie" / "install.sh"
    rel_script.write_text("#!/bin/bash\necho relative-marker\n")
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"provision_script": Path("./.gie/install.sh")},
            ),
        }
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)

    copy_call = _find_install_copy_call(incus)
    match = re.search(r"echo '([^']+)'", copy_call.args[1][2])
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode()
    assert "relative-marker" in decoded


def test_build_passes_empty_extra_apt_packages_env(make_cfg, tmp_path, mocker):
    """Default config: EXTRA_APT_PACKAGES env var is present but empty so
    install.sh's `: "${EXTRA_APT_PACKAGES:=}"` and conditional install
    block work consistently across builds."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)
    install_call = _find_install_run_call(incus)
    env = install_call.kwargs["env"]
    assert env["EXTRA_APT_PACKAGES"] == ""


def test_build_passes_extra_apt_packages_env(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"extra_apt_packages": ["mariadb-client", "postgresql-client"]},
            ),
        }
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)
    install_call = _find_install_run_call(incus)
    env = install_call.kwargs["env"]
    assert env["EXTRA_APT_PACKAGES"] == "mariadb-client postgresql-client"


def test_build_provision_env_merged_into_exec(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"provision_env": {"EXTRA_PKG": "redis"}},
            ),
        }
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg, incus)
    install_call = _find_install_run_call(incus)
    env = install_call.kwargs["env"]
    assert env["EXTRA_PKG"] == "redis"
    assert "CONTAINER_USER" not in env
    assert env["CONTAINER_UID"] == str(cfg.container_user.uid)


# ---------- Build container cleanup on failure / orphan recovery


def test_build_deletes_container_when_provisioning_fails():
    """If a provisioning step raises, the build container must still be
    cleaned up so the next `gie base build` doesn't hit a UNIQUE
    constraint failure on the same name.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    incus.exec.side_effect = RuntimeError("provisioning boom")

    with pytest.raises(RuntimeError, match="provisioning boom"):
        build_golden_image(cfg, incus)

    expected = f"{cfg.container_prefix}-base-build"
    incus.delete.assert_called_once_with(expected, force=True)


def test_build_removes_orphan_container_before_launch():
    """Recover from a previous failed build that left the container
    behind: detect on entry and delete before launching the new one.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = True
    incus.image_exists.return_value = False

    build_golden_image(cfg, incus)

    expected = f"{cfg.container_prefix}-base-build"
    # Two deletes: preflight (orphan) and post-publish cleanup.
    assert incus.delete.call_count == 2
    assert all(c == ((expected,), {"force": True}) for c in incus.delete.call_args_list)

    # Preflight delete must precede launch; otherwise the launch still
    # fails on the leftover container name.
    timeline = [c[0] for c in incus.mock_calls]
    first_delete = timeline.index("delete")
    launch_idx = timeline.index("launch")
    assert first_delete < launch_idx


# ---------- Phase B Task 3: install.d staging --------------------------------


@pytest.fixture
def cfg_for_golden(tmp_path, make_cfg):
    """Minimal Config wired for build_golden_image.

    Writes a real (legacy) `.gie/config.yaml` file so `repo_config_dir_name`
    resolves this fixture's repo to `.gie` — an empty directory alone no
    longer signals which config dir a repo uses.
    """
    repo_root = tmp_path / "myrepo"
    (repo_root / ".gie").mkdir(parents=True)
    (repo_root / ".gie" / "config.yaml").write_text("")
    cfg = make_cfg(repo_root)
    return cfg


def test_build_passes_new_env_vars(cfg_for_golden, mocker):
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg_for_golden, incus)

    install_sh_exec = next(
        c for c in incus.exec.call_args_list if c.args[1] == ["bash", "/provision/install.sh"]
    )
    env = install_sh_exec.kwargs["env"]
    assert env["JAILBEE_USER_HOME"] == "/home/dev"
    assert env["JAILBEE_PROVISION_DIR"] == "/provision"


def test_build_stages_install_d_directory(cfg_for_golden, mocker, tmp_path):
    """When a repo has .gie/install.d/foo.sh, it must be pushed to
    /provision/install.d/foo.sh in the build container.
    """
    repo_snippet = cfg_for_golden.repo_root / ".gie" / "install.d" / "55-custom.sh"
    repo_snippet.parent.mkdir(parents=True)
    repo_snippet.write_text("#!/bin/bash\necho hi\n")

    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg_for_golden, incus)

    # Find the exec call that writes the snippet
    snippet_write = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and any("/provision/install.d/55-custom.sh" in str(arg) for arg in c.args[1])
    ]
    assert snippet_write, "expected an exec call pushing the snippet to /provision/install.d/"


def test_build_skips_install_d_when_provision_script_override(cfg_for_golden, mocker, tmp_path):
    """If golden.provision_script is set, install.d/ is NOT staged."""
    custom_script = tmp_path / "custom-install.sh"
    custom_script.write_text("#!/bin/bash\nexit 0\n")
    object.__setattr__(cfg_for_golden.golden, "provision_script", custom_script)

    # Even if a repo snippet exists, it shouldn't be staged when an override is set
    repo_snippet = cfg_for_golden.repo_root / ".gie" / "install.d" / "55-custom.sh"
    repo_snippet.parent.mkdir(parents=True)
    repo_snippet.write_text("#!/bin/bash\n")

    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg_for_golden, incus)

    snippet_writes = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and any("/provision/install.d/" in str(arg) for arg in c.args[1])
    ]
    assert not snippet_writes, (
        f"no install.d/ staging when provision_script is set; got: {snippet_writes}"
    )


def test_build_stages_user_install_d_from_xdg_config_home(
    cfg_for_golden, mocker, tmp_path, monkeypatch
):
    """The user's install.d must be read where the rest of jailbee writes it.

    `global_config.default_global_config_path()` honours `XDG_CONFIG_HOME`, so
    a hardcoded `~/.config/jailbee/install.d` here would silently ignore every
    snippet belonging to a user who sets that variable — with no error, just
    provisioning that quietly skips their customisations.
    """
    xdg_config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    user_snippet = xdg_config / "jailbee" / "install.d" / "60-user.sh"
    user_snippet.parent.mkdir(parents=True)
    user_snippet.write_text("#!/bin/bash\necho user\n")

    incus = mocker.MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    build_golden_image(cfg_for_golden, incus)

    assert "60-user.sh" in _staged_snippet_names(incus)


def _merged_exec_env(incus: MagicMock) -> dict[str, str]:
    merged: dict[str, str] = {}
    for c in incus.exec.call_args_list:
        merged.update(c.kwargs.get("env", {}) or {})
    return merged


def _staged_snippet_names(incus: MagicMock) -> list[str]:
    names: list[str] = []
    for c in incus.exec.call_args_list:
        if len(c.args) > 1 and isinstance(c.args[1], list):
            m = re.search(r"/provision/install\.d/(\S+\.sh)", " ".join(c.args[1]))
            if m:
                names.append(m.group(1))
    return names


def _fresh_incus() -> MagicMock:
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    return incus


def test_build_env_uses_stack_java_and_node():
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(update={"stacks": Stacks(java="openjdk-21", node=22)})
        }
    )
    incus = _fresh_incus()
    build_golden_image(cfg, incus)
    env = _merged_exec_env(incus)
    assert env["JAVA_PACKAGE"] == "openjdk-21-jdk"
    assert env["NODE_MAJOR"] == "22"


def test_build_stages_stack_snippets():
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"stacks": Stacks(java="corretto-17", docker=True)}
            )
        }
    )
    incus = _fresh_incus()
    build_golden_image(cfg, incus)
    staged = _staged_snippet_names(incus)
    assert "20-corretto.sh" in staged
    assert "50-docker.sh" in staged
    assert "90-registry-mirror-ca.sh" in staged


def test_build_stages_stack_snippets_respects_disable_snippets_opt_out():
    """D5: the auto-added `90-registry-mirror-ca` snippet (fired when both
    java and docker stacks are on) can be opted out of via
    `golden.disable_snippets: ["90-registry-mirror-ca"]`, same as any other
    bundled snippet — the stack-derived name isn't special-cased past the
    disable list.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={
                    "stacks": Stacks(java="corretto-17", docker=True),
                    "disable_snippets": ["90-registry-mirror-ca"],
                }
            )
        }
    )
    incus = _fresh_incus()
    build_golden_image(cfg, incus)
    staged = _staged_snippet_names(incus)
    assert "20-corretto.sh" in staged
    assert "50-docker.sh" in staged
    assert "90-registry-mirror-ca.sh" not in staged


# ---------- Task 2: find_archived_images


def _img(aliases, size=100):
    return {"aliases": [{"name": n} for n in aliases], "size": size}


def test_find_archived_images_matches_dated_aliases(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = MagicMock()
    incus.list_images.return_value = [
        _img([alias]),  # live alias — ignored
        _img([f"{alias}-2026-07-20"], 500),
        _img([f"{alias}-2026-07-18"], 300),
        _img(["some-other-image"]),  # unrelated — ignored
        _img([]),  # aliasless — ignored
    ]
    result = find_archived_images(cfg, incus)
    assert result == [
        ArchivedImage(alias=f"{alias}-2026-07-20", date=date(2026, 7, 20), size_bytes=500),
        ArchivedImage(alias=f"{alias}-2026-07-18", date=date(2026, 7, 18), size_bytes=300),
    ]


def test_find_archived_images_ignores_malformed_suffix(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = MagicMock()
    incus.list_images.return_value = [
        _img([f"{alias}-not-a-date"]),
        _img([f"{alias}-2026-7-1"]),  # not zero-padded → no match
    ]
    assert find_archived_images(cfg, incus) == []


def test_find_archived_images_empty(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.list_images.return_value = []
    assert find_archived_images(cfg, incus) == []


def test_find_all_archived_images_across_prefixes(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [
        _img(["foo-base"]),  # live — ignored
        _img(["foo-base-2026-07-20"], 500),
        _img(["bar-base-2026-07-18"], 300),
        _img(["bar-base"]),  # live — ignored
        _img(["unrelated-image"]),  # ignored
    ]
    result = find_all_archived_images(incus, ["foo-base", "bar-base"])
    assert [(a.alias, a.size_bytes) for a in result] == [
        ("foo-base-2026-07-20", 500),
        ("bar-base-2026-07-18", 300),
    ]
    # sorted newest-first across prefixes
    assert result[0].date >= result[1].date


def test_find_all_archived_images_dedups_base_aliases(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [_img(["foo-base-2026-07-20"], 500)]
    result = find_all_archived_images(incus, ["foo-base", "foo-base"])
    assert len(result) == 1  # one archive, not doubled by the repeated base alias


# ---------- Task 2: GoldenImageUsage + gather_golden_usage


def test_gather_golden_usage_classifies_live_and_archives(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [
        _img(["foo-base"], 2000),  # live
        _img(["foo-base-2026-07-20"], 500),
        _img(["foo-base-2026-07-18"], 300),
        _img(["bar-base-2026-07-01"], 100),  # bar: archive only, no live
        _img(["unrelated"], 999),  # ignored
    ]
    result = gather_golden_usage(incus, ["foo-base", "bar-base"])
    assert result[0] == GoldenImageUsage(
        base_alias="foo-base",
        live_size_bytes=2000,
        archives=[
            ArchivedImage("foo-base-2026-07-20", date(2026, 7, 20), 500),
            ArchivedImage("foo-base-2026-07-18", date(2026, 7, 18), 300),
        ],
    )
    assert result[1] == GoldenImageUsage(
        base_alias="bar-base",
        live_size_bytes=None,  # no live image for bar
        archives=[ArchivedImage("bar-base-2026-07-01", date(2026, 7, 1), 100)],
    )


def test_gather_golden_usage_live_only(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [_img(["foo-base"], 2000)]
    result = gather_golden_usage(incus, ["foo-base"])
    assert result == [GoldenImageUsage("foo-base", 2000, [])]


def test_gather_golden_usage_dedups_base_aliases(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [_img(["foo-base"], 2000)]
    result = gather_golden_usage(incus, ["foo-base", "foo-base"])
    assert len(result) == 1


def test_provisioning_runs_under_a_live_status_line(mocker):
    """install.sh's apt output is captured, so this step prints nothing for
    minutes. It is the one place in the build that needs a spinner."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False
    calls: list[str] = []
    # MagicMock already implements the context-manager protocol.
    status = mocker.MagicMock()
    mocker.patch(
        "jailbee.golden.status_with_elapsed",
        side_effect=lambda message: calls.append(message) or status,
    )

    build_golden_image(cfg, incus)

    assert len(calls) == 1
    assert "provisioning script" in calls[0]


def test_provisioning_failure_still_deletes_the_build_container():
    """The status line must not swallow the failure or leak the container."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.exists.return_value = False
    incus.image_exists.return_value = False

    def fail_on_install(name, cmd, **kwargs):
        # Keyed on the command rather than on a call index: the build runs a
        # varying number of setup execs before this one.
        if cmd == ["bash", "/provision/install.sh"]:
            raise IncusError("apt-get: no such package")
        return ""

    incus.exec.side_effect = fail_on_install

    with pytest.raises(IncusError, match="no such package"):
        build_golden_image(cfg, incus)

    assert incus.delete.called
    assert incus.delete.call_args.kwargs.get("force") is True


def _repo_cfg(tmp_path, make_cfg, **overrides):
    """A Config whose repo_root has a real `.jailbee/config.yaml`.

    `repo_config_dir_name` picks the directory by the *file's* presence, so an
    empty directory is not enough — without the file, a repo snippet written
    under `.jailbee/install.d/` would be looked for under `.gie/install.d/`.
    """
    repo_root = tmp_path / "repo"
    (repo_root / ".jailbee").mkdir(parents=True)
    (repo_root / ".jailbee" / "config.yaml").write_text("")
    return make_cfg(repo_root, **overrides)


def test_resolved_snippet_names_sees_stack_docker(tmp_path, make_cfg):
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"stacks": {"docker": True}})
    assert "docker" in resolved_snippet_names(cfg)


def test_resolved_snippet_names_sees_enable_snippets_escape_hatch(tmp_path, make_cfg):
    """`golden.stacks` is sugar over `enable_snippets`; both must be visible."""
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"enable_snippets": ["50-docker"]})
    assert "docker" in resolved_snippet_names(cfg)


def test_resolved_snippet_names_sees_a_repo_owned_snippet(tmp_path, make_cfg):
    cfg = _repo_cfg(tmp_path, make_cfg)
    snippet = cfg.repo_root / ".jailbee" / "install.d" / "50-docker.sh"
    snippet.parent.mkdir(parents=True)
    snippet.write_text("#!/bin/bash\n")
    assert "docker" in resolved_snippet_names(cfg)


def test_resolved_snippet_names_honours_disable_snippets(tmp_path, make_cfg):
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"stacks": {"docker": True}, "disable_snippets": ["docker"]},
    )
    assert "docker" not in resolved_snippet_names(cfg)


def test_resolved_snippet_paths_empty_when_provision_script_overrides(tmp_path, make_cfg):
    """A custom provision script stages no install.d snippets at all."""
    script = tmp_path / "custom-install.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"provision_script": str(script), "stacks": {"docker": True}},
    )
    assert resolved_snippet_paths(cfg) == []


def test_repo_uses_docker_falls_back_to_the_stack_bool_with_provision_script(tmp_path, make_cfg):
    """install.d is bypassed, so the sugar bool is the only signal left."""
    script = tmp_path / "custom-install.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"provision_script": str(script), "stacks": {"docker": True}},
    )
    assert repo_uses_docker(cfg) is True


def test_repo_uses_docker_false_for_a_plain_repo(tmp_path, make_cfg):
    assert repo_uses_docker(_repo_cfg(tmp_path, make_cfg)) is False


def test_repo_uses_docker_sees_extra_apt_packages(tmp_path, make_cfg):
    """`docker.io` from the archive lands in the image via 05-extra-apt.sh
    without the `docker` snippet ever resolving."""
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"extra_apt_packages": ["curl", "docker.io"]})
    assert repo_uses_docker(cfg) is True


def test_repo_uses_docker_sees_extra_apt_packages_with_a_provision_script(tmp_path, make_cfg):
    """extra_apt_packages is independent of install.d, so the signal survives
    the branch where a custom provision script replaces install.sh."""
    script = tmp_path / "custom-install.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"provision_script": str(script), "extra_apt_packages": ["docker-ce"]},
    )
    assert repo_uses_docker(cfg) is True


def test_repo_uses_docker_ignores_unrelated_extra_apt_packages(tmp_path, make_cfg):
    """The prefix test must not fire on packages that merely mention docker
    late in the name."""
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"extra_apt_packages": ["golang-docker-dev"]})
    assert repo_uses_docker(cfg) is False
