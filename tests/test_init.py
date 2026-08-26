"""Tests for `gie init`."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.config import load_config
from jailbee.incus import IncusError
from jailbee.init_command import apply_allowlist_acl, run_init
from tests.conftest import make_cfg, with_agent

FIXTURES = Path(__file__).parent / "fixtures"

# Real stderr from a host hitting the quirk — Incus 6.0.4's nftables backend
# to flush a chain that hasn't been created yet.
NFT_FLUSH_MISSING_STDERR = (
    "`incus network acl edit tests-allowlist` failed: "
    "Error: Failed to run: nft -f -: exit status 1 "
    "(/dev/stdin:2:24-35: Error: No such file or directory; "
    "did you mean chain 'fwd.incusbr0' in table inet 'incus'?  "
    "flush chain inet incus acl.incusbr0"
)


def test_init_creates_shared_dirs(tmp_path):
    """With claude and jetbrains disabled, neither claude nor jetbrains
    subdirs are created. Other shared subdirs are always created."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    # Drop the fixture's claude.enabled + jetbrains.enabled flags for this test.
    cfg = with_agent(cfg, "claude", enabled=False)
    cfg = cfg.model_copy(update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": False})})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    expected = [
        "caches/pnpm-store",
        "caches/gradle",
        "caches/npm",
        "caches/m2",
        "chrome-pool/slots",
        "chrome-pool/by-container",
        "docker-registry",
        "ssh",
    ]
    for sub in expected:
        assert (tmp_path / "shared" / sub).is_dir(), f"Missing {sub}"
    assert not (tmp_path / "shared" / "claude").exists()
    assert not (tmp_path / "shared" / "claude-install").exists()
    assert not (tmp_path / "shared" / "jetbrains-config").exists()
    assert not (tmp_path / "shared" / "jetbrains-data").exists()


def test_run_init_does_not_create_jetbrains_subdirs_when_disabled(tmp_path):
    """With jetbrains.enabled=false, <shared_dir>/jetbrains-* are NOT
    created (mirrors the claude gating)."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    cfg = cfg.model_copy(update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": False})})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert not (tmp_path / "shared" / "jetbrains-config").exists()
    assert not (tmp_path / "shared" / "jetbrains-data").exists()


def test_run_init_creates_jetbrains_subdirs_when_enabled(tmp_path):
    """With jetbrains.enabled, <shared_dir>/jetbrains-{config,data} are
    created."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    # full_config.yaml sets jetbrains.enabled: true already.
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert (tmp_path / "shared" / "jetbrains-config").is_dir()
    assert (tmp_path / "shared" / "jetbrains-data").is_dir()


def test_run_init_creates_jetbrains_idea_subdir_when_enabled(tmp_path):
    """With jetbrains.enabled + share_idea (default), <shared_dir>/jetbrains-idea
    is created so the per-container bind has a source path."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert (tmp_path / "shared" / "jetbrains-idea").is_dir()


def test_run_init_does_not_create_jetbrains_idea_subdir_when_share_idea_off(tmp_path):
    """share_idea=False suppresses just the .idea subdir; config/data still
    appear because jetbrains.enabled is still on."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    cfg = cfg.model_copy(
        update={"jetbrains": cfg.jetbrains.model_copy(update={"share_idea": False})}
    )
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert not (tmp_path / "shared" / "jetbrains-idea").exists()
    assert (tmp_path / "shared" / "jetbrains-config").is_dir()
    assert (tmp_path / "shared" / "jetbrains-data").is_dir()


def test_run_init_does_not_create_jetbrains_idea_subdir_when_jetbrains_disabled(tmp_path):
    """jetbrains.enabled=False suppresses all jetbrains subdirs."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    cfg = cfg.model_copy(update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": False})})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert not (tmp_path / "shared" / "jetbrains-idea").exists()


def test_run_init_creates_claude_subdir_when_enabled(tmp_path):
    """With claude.enabled, <shared_dir>/claude is created."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    # full_config.yaml sets claude.enabled: true already.
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert (tmp_path / "shared" / "claude").is_dir()


def test_run_init_creates_claude_install_subdir_when_enabled(tmp_path):
    """With claude.enabled, <shared_dir>/claude-install is created as the
    shared Claude version-store bind source."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    # full_config.yaml sets claude.enabled: true already.
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert (tmp_path / "shared" / "claude-install").is_dir()


def test_run_init_creates_user_shared_cache_dirs(tmp_path):
    """User-defined `shared_caches` host_subpaths are created as
    directories so `incus profile assign` doesn't fail validation with a
    missing disk source path (issue: pebble-oauth)."""
    from jailbee.config import SharedCache

    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    cfg = cfg.model_copy(
        update={
            "shared_caches": [
                SharedCache(
                    name="pebble-oauth",
                    host_subpath="pebble-oauth",
                    container_path="~/.config/pebble/oauth",
                ),
            ]
        }
    )
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert (tmp_path / "shared" / "pebble-oauth").is_dir()


def test_run_init_skips_claude_json_touch_when_disabled(tmp_path):
    """The claude.json seed file is only written when claude.enabled — and
    with it disabled, a pre-existing legacy `claude.json` must also be left
    untouched (i.e. the relocation half is gated too, not just the seed)."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    cfg = with_agent(cfg, "claude", enabled=False)
    (tmp_path / "shared").mkdir(parents=True)
    (tmp_path / "shared" / "claude.json").write_text('{"legacy": true}')
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert not (tmp_path / "shared" / "claude" / ".claude.json").exists()
    assert (tmp_path / "shared" / "claude.json").read_text() == '{"legacy": true}'


def test_init_creates_profiles(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    p = cfg.container_prefix
    created = [call.args[0] for call in incus.profile_create.call_args_list]
    assert f"{p}-base" in created
    assert f"{p}-binds" in created
    assert f"{p}-net-strict" in created
    assert f"{p}-net-loose" in created


def test_init_creates_acl(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    incus.network_acl_create.assert_called_with(f"{cfg.container_prefix}-allowlist")
    incus.network_acl_set_yaml.assert_called_once()


def test_init_applies_acl_before_strict_net_profile(tmp_path):
    """Regression: <repo>-net-strict references ACL `<repo>-allowlist` via
    security.acls, so the ACL must exist before the strict net profile is
    edited. Otherwise Incus rejects the eth0 device.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    strict_name = f"{cfg.container_prefix}-net-strict"
    timeline = incus.mock_calls
    acl_idx = next(i for i, c in enumerate(timeline) if c[0] == "network_acl_set_yaml")
    strict_idx = next(
        i for i, c in enumerate(timeline) if c[0] == "profile_set_yaml" and c.args[0] == strict_name
    )
    assert acl_idx < strict_idx, (
        f"ACL set_yaml (idx {acl_idx}) must come before "
        f"{strict_name} profile set_yaml (idx {strict_idx})"
    )


def test_init_raises_when_profile_already_exists(tmp_path):
    """Init is first-time-setup only — use `gie apply` for updates."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = True
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    with pytest.raises(RuntimeError, match="jailbee apply"):
        run_init(cfg, incus)


def test_init_raises_when_acl_already_exists(tmp_path):
    """Same first-time-only semantic for ACLs as for profiles."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = True
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    with pytest.raises(RuntimeError, match="jailbee apply"):
        run_init(cfg, incus)


def test_init_acl_edit_nft_flush_chain_missing_swallowed_on_first_run(tmp_path):
    """On a fresh host the nftables flush of `acl.incusbr0` can
    fail because the chain doesn't exist yet — the ACL is persisted in
    Incus's database, only the live nftables sync is missing. Treat as a
    warning and continue, so net profiles still get applied.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True
    incus.network_acl_set_yaml.side_effect = IncusError(NFT_FLUSH_MISSING_STDERR)

    # Should NOT raise.
    run_init(cfg, incus)

    p = cfg.container_prefix
    profile_set_names = [c.args[0] for c in incus.profile_set_yaml.call_args_list]
    assert f"{p}-net-strict" in profile_set_names
    assert f"{p}-net-loose" in profile_set_names


def test_init_acl_edit_reraises_other_errors(tmp_path):
    """Only the specific nftables-flush-missing pattern is swallowed."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True
    incus.network_acl_set_yaml.side_effect = IncusError("Error: yaml: invalid syntax at line 5")

    with pytest.raises(IncusError, match="invalid syntax"):
        run_init(cfg, incus)


# ---- _ensure_shared_owner ----


def test_ensure_shared_owner_writes_file_first_time(tmp_path):
    from jailbee.init_command import _ensure_shared_owner

    shared = tmp_path / "shared"
    repo = tmp_path / "myrepo"
    repo.mkdir()

    _ensure_shared_owner(shared, repo)

    owner_file = shared / ".owner"
    assert owner_file.read_text().strip() == str(repo)


def test_ensure_shared_owner_passes_when_owner_matches(tmp_path):
    from jailbee.init_command import _ensure_shared_owner

    shared = tmp_path / "shared"
    repo = tmp_path / "myrepo"
    repo.mkdir()

    _ensure_shared_owner(shared, repo)
    _ensure_shared_owner(shared, repo)  # second call must not error

    assert (shared / ".owner").read_text().strip() == str(repo)


def test_ensure_shared_owner_collision_raises(tmp_path):
    from jailbee.config import ConfigError
    from jailbee.init_command import _ensure_shared_owner

    shared = tmp_path / "shared"
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    _ensure_shared_owner(shared, repo_a)

    with pytest.raises(ConfigError) as exc_info:
        _ensure_shared_owner(shared, repo_b)

    assert str(repo_a) in str(exc_info.value)
    assert str(repo_b) in str(exc_info.value)
    assert "collision" in str(exc_info.value).lower()


# ---- per-repo profile/ACL names ----


def test_run_init_creates_per_repo_profiles(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, shared_dir=tmp_path / "shared")
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    created = [call.args[0] for call in incus.profile_create.call_args_list]
    assert "myrepo-base" in created
    assert "myrepo-binds" in created
    assert "myrepo-net-strict" in created
    assert "myrepo-net-loose" in created

    acl_created = [call.args[0] for call in incus.network_acl_create.call_args_list]
    assert acl_created == ["myrepo-allowlist"]


# ---- ensure_acl_attached_to_bridge ----


def test_init_attaches_acl_to_bridge_when_missing(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    name = f"{cfg.container_prefix}-allowlist"
    incus.network_set.assert_called_once_with(
        "incusbr0",
        "security.acls",
        name,
    )


def test_init_skips_attach_when_already_present(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = f"{cfg.container_prefix}-allowlist"
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    incus.network_set.assert_not_called()


def test_init_preserves_other_repo_acls(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = "otherrepo-allowlist"
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    name = f"{cfg.container_prefix}-allowlist"
    incus.network_set.assert_called_once_with(
        "incusbr0",
        "security.acls",
        f"otherrepo-allowlist,{name}",
    )


def test_init_attaches_after_acl_created(tmp_path):
    """The ACL must exist as an Incus object before `network set` can
    reference it — otherwise Incus rejects with 'Network ACL ... does not
    exist'. Mirrors `test_init_applies_acl_before_strict_net_profile`.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    timeline = incus.mock_calls
    acl_idx = next(i for i, c in enumerate(timeline) if c[0] == "network_acl_set_yaml")
    attach_idx = next(i for i, c in enumerate(timeline) if c[0] == "network_set")
    assert acl_idx < attach_idx, (
        f"ACL set_yaml (idx {acl_idx}) must come before network_set (idx {attach_idx})"
    )


# ---- ensure_loose_bridge ----


def test_init_creates_loose_bridge_when_missing(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = False

    run_init(cfg, incus)

    incus.network_create.assert_called_once_with("jailbee-loose")


def test_init_skips_bridge_when_already_exists(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    incus.network_create.assert_not_called()


def test_init_creates_bridge_before_loose_net_profile(tmp_path):
    """The `<prefix>-net-loose` profile references `jailbee-loose` via the
    `network:` key — the bridge must exist as an Incus object before the
    profile edit, otherwise Incus rejects with 'Network ... does not exist'.
    Mirrors `test_init_attaches_after_acl_created`.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = False

    run_init(cfg, incus)

    loose_name = f"{cfg.container_prefix}-net-loose"
    timeline = incus.mock_calls
    create_idx = next(
        i
        for i, c in enumerate(timeline)
        if c[0] == "network_create" and c.args[0] == "jailbee-loose"
    )
    loose_idx = next(
        i for i, c in enumerate(timeline) if c[0] == "profile_set_yaml" and c.args[0] == loose_name
    )
    assert create_idx < loose_idx, (
        f"network_create('jailbee-loose') (idx {create_idx}) must come before "
        f"{loose_name} profile_set_yaml (idx {loose_idx})"
    )


def test_apply_allowlist_acl_passes_mirror_endpoint_through(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.network_acl_exists.return_value = False  # first-time setup

    spy = mocker.patch(
        "jailbee.init_command.allowlist_acl_yaml",
        return_value="name: stub\n",
    )

    apply_allowlist_acl(
        cfg,
        incus,
        entries=None,
        mirror_endpoint=("10.234.216.1", 15000),
    )

    _args, kwargs = spy.call_args
    # network.allowlist_acl_yaml's positional layout is (cfg, entries, mirror_endpoint).
    # apply_allowlist_acl is free to pass either positionally or by keyword;
    # accept both.
    if "mirror_endpoint" in kwargs:
        assert kwargs["mirror_endpoint"] == ("10.234.216.1", 15000)
    else:
        assert spy.call_args.args[-1] == ("10.234.216.1", 15000)


def test_run_init_forwards_mirror_endpoint_to_allowlist_acl(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, shared_dir=tmp_path / "shared")
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    spy = mocker.patch("jailbee.init_command.apply_allowlist_acl")

    run_init(cfg, incus, mirror_endpoint=("10.234.216.1", 15000))

    spy.assert_called_once()
    kwargs = spy.call_args.kwargs
    assert kwargs.get("mirror_endpoint") == ("10.234.216.1", 15000)


# ---- Claude bind-mount source preparation ----


def test_run_init_creates_empty_claude_json_when_enabled(make_cfg, tmp_path):
    """The `claude/.claude.json` seed must exist or Claude Code's first
    invocation (run by the Claude Code installer) aborts with a parse error
    on a zero-byte/missing file, hard-failing `gie new`. `gie init` seeds
    valid empty JSON (`{}`) when claude.enabled."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(
        repo,
        shared_dir=tmp_path / "shared",
        claude={"enabled": True},
    )
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    json_path = tmp_path / "shared" / "claude" / ".claude.json"
    assert json_path.is_file()
    assert json_path.read_text() == "{}\n"


def test_run_init_does_not_overwrite_existing_claude_json(make_cfg, tmp_path):
    """Pre-existing <shared_dir>/claude/.claude.json (e.g. previously written
    by a container) must be left untouched by re-init."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "claude").mkdir()
    (shared / "claude" / ".claude.json").write_text('{"existing": true}')
    cfg = make_cfg(
        repo,
        shared_dir=shared,
        claude={"enabled": True},
    )
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    assert (shared / "claude" / ".claude.json").read_text() == '{"existing": true}'


def test_agent_dir_and_file_mounts_are_created(tmp_path):
    from jailbee.init_command import _ensure_integration_shared_dirs

    shared = tmp_path / "shared"
    shared.mkdir()
    cfg = make_cfg(
        tmp_path,
        shared_dir=shared,
        agents={"aider": {"enabled": True}, "codex": {"enabled": True}},
    )
    _ensure_integration_shared_dirs(cfg)
    assert (shared / "codex").is_dir()
    assert (shared / "aider.conf.yml").is_file()
    assert (shared / "aider.conf.yml").read_text() == ""


def test_claude_json_seeded_inside_the_claude_dir(tmp_path):
    from jailbee.init_command import _ensure_integration_shared_dirs

    shared = tmp_path / "shared"
    shared.mkdir()
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})
    _ensure_integration_shared_dirs(cfg)
    assert (shared / "claude" / ".claude.json").read_text() == "{}\n"
    assert not (shared / "claude.json").exists()


def test_run_init_chmods_ssh_to_0700_when_ssh_enabled(make_cfg, tmp_path, mocker):
    """SSH refuses ~/.ssh with group/world bits. Chmod runs whenever
    ssh.enabled is true, regardless of seed_from_host."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    shared = tmp_path / "shared"
    cfg = make_cfg(repo, shared_dir=shared, ssh={"enabled": True, "seed_from_host": False})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    run_init(cfg, incus)

    mode = (shared / "ssh").stat().st_mode & 0o777
    assert mode == 0o700, f"Expected mode 0700, got {oct(mode)}"


def test_run_init_seeds_ssh_when_enabled_and_target_empty(make_cfg, tmp_path, mocker):
    """run_init calls seed_ssh_dir with the target and host paths."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, shared_dir=tmp_path / "shared", ssh={"enabled": True})
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    spy = mocker.patch(
        "jailbee.init_command.seed_ssh_dir",
        return_value=2,
    )

    run_init(cfg, incus)

    spy.assert_called_once()
    args = spy.call_args.args
    assert args[0] == tmp_path / "shared" / "ssh"
    assert args[1] == Path.home() / ".ssh"


def test_run_init_skips_ssh_seed_when_seed_from_host_false(make_cfg, tmp_path, mocker):
    """ssh.seed_from_host=false short-circuits the seed call."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(
        repo, shared_dir=tmp_path / "shared", ssh={"enabled": True, "seed_from_host": False}
    )
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    spy = mocker.patch("jailbee.init_command.seed_ssh_dir")

    run_init(cfg, incus)

    spy.assert_not_called()


def test_run_init_skips_ssh_seed_when_ssh_disabled(make_cfg, tmp_path, mocker):
    """ssh.enabled=false short-circuits the seed call regardless of seed_from_host."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(
        repo, shared_dir=tmp_path / "shared", ssh={"enabled": False, "seed_from_host": True}
    )
    incus = MagicMock()
    incus.profile_exists.return_value = False
    incus.network_acl_exists.return_value = False
    incus.network_get.return_value = ""
    incus.network_exists.return_value = True

    spy = mocker.patch("jailbee.init_command.seed_ssh_dir")

    run_init(cfg, incus)

    spy.assert_not_called()


def test_relocate_claude_json_moves_a_legacy_file(tmp_path):
    from jailbee.init_command import _relocate_claude_json

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    (shared / "claude.json").write_text('{"real": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    assert (shared / "claude" / ".claude.json").read_text() == '{"real": true}'
    assert not (shared / "claude.json").exists()


def test_relocate_claude_json_announces_the_move(mocker, tmp_path):
    """The migration touches the user's Claude identity — it must say so,
    not move it silently (mirrors the SSH seed's `success(...)` announcement)."""
    from rich.console import Console

    from jailbee.init_command import _relocate_claude_json

    recording = Console(record=True, width=200)
    mocker.patch("jailbee.tui.console", recording)

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    source = shared / "claude.json"
    source.write_text('{"real": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    out = recording.export_text()
    assert str(source) in out
    assert str(shared / "claude" / ".claude.json") in out


def test_relocate_claude_json_warns_when_orphaning_the_legacy_file(mocker, tmp_path):
    """The skip-because-destination-exists branch is finding 2's orphaning
    case (a later `apply` can never migrate the legacy file again) and must
    warn, naming both paths, rather than silently leaving the user unaware."""
    from rich.console import Console

    from jailbee.init_command import _relocate_claude_json

    recording = Console(record=True, width=200)
    mocker.patch("jailbee.tui.console", recording)

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    source = shared / "claude.json"
    source.write_text('{"old": true}')
    (shared / "claude" / ".claude.json").write_text('{"current": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    out = recording.export_text()
    assert str(source) in out
    assert str(shared / "claude" / ".claude.json") in out


def test_relocate_claude_json_skips_a_symlink_source(mocker, tmp_path):
    """A symlink source must not be renamed: `rename()` moves the link, not
    its target, leaving a dangling symlink at the destination inside the
    container."""
    from rich.console import Console

    from jailbee.init_command import _relocate_claude_json

    recording = Console(record=True, width=200)
    mocker.patch("jailbee.tui.console", recording)

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    real = tmp_path / "real-claude.json"
    real.write_text('{"real": true}')
    source = shared / "claude.json"
    source.symlink_to(real)
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    assert source.is_symlink()
    assert not (shared / "claude" / ".claude.json").exists()
    out = recording.export_text()
    assert str(source) in out


def test_relocate_claude_json_is_a_noop_without_a_legacy_file(tmp_path):
    from jailbee.init_command import _relocate_claude_json

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    assert not (shared / "claude" / ".claude.json").exists()


def test_ensure_claude_config_dir_sets_the_key_when_absent(tmp_path):
    from jailbee.init_command import ensure_claude_config_dir

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    incus = MagicMock()
    incus.profile_exists.return_value = True
    incus.profile_config_get.return_value = None

    ensure_claude_config_dir(cfg, incus)

    incus.profile_config_set.assert_called_once_with(
        f"{cfg.container_prefix}-base",
        "environment.CLAUDE_CONFIG_DIR",
        "/home/dev/.claude",
    )


def test_ensure_claude_config_dir_respects_a_container_env_override(tmp_path):
    """`base_profile_yaml` lets `container.env` override the default, so the
    repair must write the same value — otherwise the two writers disagree and
    the next `jailbee apply` silently changes it back."""
    from jailbee.init_command import ensure_claude_config_dir

    cfg = make_cfg(
        tmp_path,
        agents={"claude": {"enabled": True}},
        container={"env": {"CLAUDE_CONFIG_DIR": "/opt/claude-config"}},
    )
    incus = MagicMock()
    incus.profile_exists.return_value = True
    incus.profile_config_get.return_value = None

    ensure_claude_config_dir(cfg, incus)

    incus.profile_config_set.assert_called_once_with(
        f"{cfg.container_prefix}-base",
        "environment.CLAUDE_CONFIG_DIR",
        "/opt/claude-config",
    )


def test_ensure_claude_config_dir_leaves_an_existing_value_alone(tmp_path):
    from jailbee.init_command import ensure_claude_config_dir

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    incus = MagicMock()
    incus.profile_exists.return_value = True
    incus.profile_config_get.return_value = "/somewhere/else"

    ensure_claude_config_dir(cfg, incus)

    incus.profile_config_set.assert_not_called()


def test_ensure_claude_config_dir_skips_a_repo_without_a_base_profile(tmp_path):
    """Nothing to repair before `jailbee init` — and `profile show` on
    a missing profile is an error, not an empty answer."""
    from jailbee.init_command import ensure_claude_config_dir

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    incus = MagicMock()
    incus.profile_exists.return_value = False

    ensure_claude_config_dir(cfg, incus)

    incus.profile_config_get.assert_not_called()
    incus.profile_config_set.assert_not_called()


def test_relocate_claude_json_never_overwrites_the_destination(tmp_path):
    """Both files present: the destination is live state, the source is a
    leftover. Never overwrite, and never delete the user's copy either."""
    from jailbee.init_command import _relocate_claude_json

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    (shared / "claude.json").write_text('{"old": true}')
    (shared / "claude" / ".claude.json").write_text('{"current": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    assert (shared / "claude" / ".claude.json").read_text() == '{"current": true}'
    assert (shared / "claude.json").read_text() == '{"old": true}'


def test_relocate_claude_json_creates_a_missing_claude_dir(tmp_path):
    from jailbee.init_command import _relocate_claude_json

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "claude.json").write_text('{"real": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _relocate_claude_json(cfg)

    assert (shared / "claude" / ".claude.json").read_text() == '{"real": true}'


def test_seed_claude_json_writes_an_empty_object(tmp_path):
    from jailbee.init_command import _seed_claude_json

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _seed_claude_json(cfg)

    assert (shared / "claude" / ".claude.json").read_text() == "{}\n"


def test_seed_claude_json_does_not_touch_an_existing_file(tmp_path):
    from jailbee.init_command import _seed_claude_json

    shared = tmp_path / "shared"
    (shared / "claude").mkdir(parents=True)
    (shared / "claude" / ".claude.json").write_text('{"existing": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _seed_claude_json(cfg)

    assert (shared / "claude" / ".claude.json").read_text() == '{"existing": true}'


def test_legacy_claude_json_survives_seeding(tmp_path):
    """Relocation must run before the seed. If the seed wins, `{}` lands at
    the destination, the relocation no-ops on a now-existing target, and the
    user's real Claude state is orphaned at the old path."""
    from jailbee.init_command import _ensure_integration_shared_dirs

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "claude.json").write_text('{"onboarded": true}')
    cfg = make_cfg(tmp_path, shared_dir=shared, agents={"claude": {"enabled": True}})

    _ensure_integration_shared_dirs(cfg)

    assert (shared / "claude" / ".claude.json").read_text() == '{"onboarded": true}'
    # Pins move-not-copy: a copy-instead-of-move implementation would still
    # pass the assertion above but leave the legacy file behind.
    assert not (shared / "claude.json").exists()
