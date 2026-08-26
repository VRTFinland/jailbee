"""Tests for Config.effective_host_mounts auto-add + manual-wins logic."""

from __future__ import annotations

from pathlib import Path

from jailbee.config import HostMount
from tests.conftest import make_cfg


def _container_paths(mounts: list[HostMount]) -> list[str]:
    return [str(m.container) for m in mounts]


def test_gpg_enabled_adds_gnupg_mount(tmp_path):
    cfg = make_cfg(tmp_path, gpg={"enabled": True})
    mounts = cfg.effective_host_mounts()
    assert "/home/dev/.gnupg" in _container_paths(mounts)


def test_gpg_disabled_omits_gnupg_mount(tmp_path):
    cfg = make_cfg(tmp_path, gpg={"enabled": False})
    mounts = cfg.effective_host_mounts()
    assert "/home/dev/.gnupg" not in _container_paths(mounts)


def test_jetbrains_userprefs_enabled_adds_mount(tmp_path):
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "userprefs_from_host": True})
    mounts = cfg.effective_host_mounts()
    userprefs_path = str(Path.home() / ".java" / ".userPrefs" / "jetbrains")
    assert userprefs_path in _container_paths(mounts)


def test_toolbox_host_path_adds_ro_mount(tmp_path):
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "toolbox_host_path": "/opt/Toolbox"})
    mounts = cfg.effective_host_mounts()
    matching = [m for m in mounts if str(m.container) == "/opt/jetbrains-toolbox"]
    assert len(matching) == 1
    assert matching[0].host == Path("/opt/Toolbox")
    assert matching[0].readonly is True


def test_toolbox_host_path_null_omits_mount(tmp_path):
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "toolbox_host_path": None})
    mounts = cfg.effective_host_mounts()
    assert "/opt/jetbrains-toolbox" not in _container_paths(mounts)


def test_manual_host_mount_wins_over_auto_add(tmp_path):
    """User-supplied entry whose container path matches an auto-add
    suppresses the auto-add; the manual entry's flags (RW etc.) are preserved.
    """
    (tmp_path / ".gnupg").mkdir()
    manual_gpg = {
        "host": str(tmp_path / ".gnupg"),
        "container": "/home/dev/.gnupg",
        "readonly": False,  # user wants RW
    }
    cfg = make_cfg(tmp_path, host_mounts=[manual_gpg], gpg={"enabled": True})
    mounts = cfg.effective_host_mounts()
    gnupg_entries = [m for m in mounts if str(m.container) == "/home/dev/.gnupg"]
    assert len(gnupg_entries) == 1
    assert gnupg_entries[0].readonly is False


def test_jetbrains_disabled_omits_all_jetbrains_auto_mounts(tmp_path):
    """jetbrains.enabled=false suppresses both userprefs and toolbox mounts
    regardless of their individual values."""
    cfg = make_cfg(
        tmp_path,
        jetbrains={
            "enabled": False,
            "userprefs_from_host": True,
            "toolbox_host_path": "/opt/Toolbox",
        },
    )
    mounts = cfg.effective_host_mounts()
    containers = _container_paths(mounts)
    assert "/opt/jetbrains-toolbox" not in containers
    assert not any("userPrefs" in c for c in containers)


def test_chrome_enabled_adds_default_host_path_mount(tmp_path):
    """chrome.enabled=true with default host_path auto-mounts /opt/google/chrome."""
    cfg = make_cfg(tmp_path, chrome={"enabled": True})
    mounts = cfg.effective_host_mounts()
    matching = [m for m in mounts if str(m.container) == "/opt/google/chrome"]
    assert len(matching) == 1
    assert matching[0].host == Path("/opt/google/chrome")
    assert matching[0].readonly is True


def test_chrome_host_path_override_adds_custom_mount(tmp_path):
    """chrome.host_path overrides the default Chrome install location."""
    cfg = make_cfg(tmp_path, chrome={"enabled": True, "host_path": "/opt/chromium"})
    mounts = cfg.effective_host_mounts()
    matching = [m for m in mounts if str(m.container) == "/opt/google/chrome"]
    assert len(matching) == 1
    assert matching[0].host == Path("/opt/chromium")
    assert matching[0].readonly is True


def test_chrome_host_path_null_omits_mount(tmp_path):
    cfg = make_cfg(tmp_path, chrome={"enabled": True, "host_path": None})
    mounts = cfg.effective_host_mounts()
    assert "/opt/google/chrome" not in _container_paths(mounts)


def test_chrome_disabled_omits_host_path_mount(tmp_path):
    """chrome.enabled=false suppresses the host_path auto-mount regardless
    of host_path being set."""
    cfg = make_cfg(tmp_path, chrome={"enabled": False, "host_path": "/opt/google/chrome"})
    mounts = cfg.effective_host_mounts()
    assert "/opt/google/chrome" not in _container_paths(mounts)


def test_manual_chrome_mount_wins_over_auto_add(tmp_path):
    (tmp_path / "chrome").mkdir()
    manual = {
        "host": str(tmp_path / "chrome"),
        "container": "/opt/google/chrome",
        "readonly": False,
    }
    cfg = make_cfg(tmp_path, host_mounts=[manual], chrome={"enabled": True})
    mounts = cfg.effective_host_mounts()
    chrome_entries = [m for m in mounts if str(m.container) == "/opt/google/chrome"]
    assert len(chrome_entries) == 1
    assert chrome_entries[0].readonly is False


def test_manual_entries_listed_before_auto_adds(tmp_path):
    (tmp_path / "manual").mkdir()
    manual = {
        "host": str(tmp_path / "manual"),
        "container": "/home/dev/custom",
        "readonly": True,
    }
    cfg = make_cfg(
        tmp_path,
        host_mounts=[manual],
        gpg={"enabled": True},
        jetbrains={"enabled": True, "toolbox_host_path": None},
    )
    mounts = cfg.effective_host_mounts()
    containers = _container_paths(mounts)
    assert containers[0] == "/home/dev/custom"
    assert "/home/dev/.gnupg" in containers[1:]


def test_effective_shared_caches_includes_claude_when_enabled(tmp_path):
    # `shared_caches=[]` keeps this test focused on the claude agent's
    # spec-driven mounts (see `agents.enabled_agent_specs`) — the defaults
    # don't contain claude entries any more, but stripping them removes a
    # noise source from the assertions.
    cfg = make_cfg(tmp_path, claude={"enabled": True}, shared_caches=[])
    caches = cfg.effective_shared_caches()
    by_name = {c.name: c for c in caches}
    assert "claude" in by_name
    assert by_name["claude"].host_subpath == "claude"
    assert by_name["claude"].container_path == "~/.claude"
    # `.claude.json` is no longer its own file-level bind; it lives inside the
    # `claude` directory mount (CLAUDE_CONFIG_DIR).
    assert "claude-json" not in by_name


def test_effective_shared_caches_excludes_claude_when_disabled(tmp_path):
    cfg = make_cfg(tmp_path, claude={"enabled": False})
    caches = cfg.effective_shared_caches()
    names = [c.name for c in caches]
    assert "claude" not in names
    assert "claude-json" not in names


def test_effective_shared_caches_manual_entry_suppresses_auto_add(tmp_path):
    """User-supplied `shared_caches` entry named `claude` wins; the auto-add
    is silently skipped (same precedent as effective_host_mounts)."""
    cfg = make_cfg(
        tmp_path,
        claude={"enabled": True},
        shared_caches=[
            {"name": "claude", "host_subpath": "my-claude", "container_path": "~/.claude"},
        ],
    )
    caches = cfg.effective_shared_caches()
    claude_entries = [c for c in caches if c.name == "claude"]
    assert len(claude_entries) == 1
    assert claude_entries[0].host_subpath == "my-claude"


def test_effective_egress_allow_includes_claude_hosts_when_enabled(tmp_path):
    from jailbee.constants import CLAUDE_API_HOSTS

    cfg = make_cfg(tmp_path, claude={"enabled": True}, egress_allow=["github.com:22"])
    allow = cfg.effective_egress_allow()
    for host in CLAUDE_API_HOSTS:
        assert host in allow
    # User entry preserved + comes first
    assert allow[0] == "github.com:22"


def test_effective_egress_allow_omits_claude_hosts_when_disabled(tmp_path):
    cfg = make_cfg(tmp_path, claude={"enabled": False}, egress_allow=["github.com:22"])
    allow = cfg.effective_egress_allow()
    assert "api.anthropic.com:443" not in allow
    assert "code.claude.com:443" not in allow
    assert "downloads.claude.ai:443" not in allow
    assert "claude.ai:443" not in allow


def test_effective_egress_allow_includes_claude_self_update_host(tmp_path):
    """Native-installer self-update needs downloads.claude.ai reachable."""
    cfg = make_cfg(tmp_path, claude={"enabled": True})
    allow = cfg.effective_egress_allow()
    assert "downloads.claude.ai:443" in allow


def test_effective_egress_allow_includes_claude_install_bootstrap_host(tmp_path):
    """`gie new` runs `curl https://claude.ai/install.sh` inside the
    (possibly strict-mode) container; claude.ai 302-redirects to
    downloads.claude.ai, so the bare claude.ai host must also be allowed
    or the first-container install is blocked at the initial request."""
    cfg = make_cfg(tmp_path, claude={"enabled": True})
    allow = cfg.effective_egress_allow()
    assert "claude.ai:443" in allow


def test_effective_egress_allow_dedupes_claude_hosts(tmp_path):
    """If the user already listed a claude host, the auto-add doesn't duplicate it."""
    cfg = make_cfg(
        tmp_path,
        claude={"enabled": True},
        egress_allow=["api.anthropic.com:443"],
    )
    allow = cfg.effective_egress_allow()
    assert allow.count("api.anthropic.com:443") == 1


def test_effective_egress_allow_includes_claude_plugin_hosts_by_default(tmp_path):
    from jailbee.constants import CLAUDE_PLUGIN_HOSTS

    cfg = make_cfg(tmp_path, claude={"enabled": True})
    allow = cfg.effective_egress_allow()
    for host in CLAUDE_PLUGIN_HOSTS:
        assert host in allow


def test_effective_egress_allow_omits_claude_plugin_hosts_when_disabled(tmp_path):
    from jailbee.constants import CLAUDE_PLUGIN_HOSTS

    cfg = make_cfg(tmp_path, claude={"enabled": True, "plugins_enabled": False})
    allow = cfg.effective_egress_allow()
    for host in CLAUDE_PLUGIN_HOSTS:
        assert host not in allow
    # API hosts still present — plugins_enabled is independent of api access.
    assert "api.anthropic.com:443" in allow


def test_effective_egress_allow_omits_plugin_hosts_when_claude_disabled(tmp_path):
    """`plugins_enabled` has no effect when the master claude switch is off."""
    from jailbee.constants import CLAUDE_PLUGIN_HOSTS

    cfg = make_cfg(tmp_path, claude={"enabled": False, "plugins_enabled": True})
    allow = cfg.effective_egress_allow()
    for host in CLAUDE_PLUGIN_HOSTS:
        assert host not in allow


def test_effective_egress_allow_includes_github_hosts_when_enabled(tmp_path):
    from jailbee.config import GITHUB_API_HOSTS

    cfg = make_cfg(
        tmp_path,
        github={"enabled": True, "api_tokens": {"prefix": "github_pat_x"}},
    )
    allow = cfg.effective_egress_allow()
    for host in GITHUB_API_HOSTS:
        assert host in allow


def test_effective_egress_allow_omits_github_hosts_when_disabled(tmp_path):
    cfg = make_cfg(tmp_path, github={"enabled": False})
    allow = cfg.effective_egress_allow()
    assert "api.github.com:443" not in allow


def test_effective_egress_allow_includes_github_hosts_without_token_entry(tmp_path):
    # Spec: egress auto-add fires on enabled=true alone, independent of
    # whether this container's prefix has a token entry.
    cfg = make_cfg(
        tmp_path,
        github={"enabled": True, "api_tokens": {"other": "github_pat_x"}},
    )
    allow = cfg.effective_egress_allow()
    assert "api.github.com:443" in allow


def test_effective_egress_allow_dedupes_github_hosts(tmp_path):
    cfg = make_cfg(
        tmp_path,
        github={"enabled": True, "api_tokens": {"prefix": "github_pat_x"}},
        egress_allow=["api.github.com:443"],
    )
    allow = cfg.effective_egress_allow()
    assert allow.count("api.github.com:443") == 1


def test_effective_shared_caches_includes_jetbrains_when_enabled(tmp_path):
    # `shared_caches=[]` isolates the test from defaults so the assertion
    # exercises only `_jetbrains_shared_caches()` + the auto-add branch.
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True}, shared_caches=[])
    caches = cfg.effective_shared_caches()
    by_name = {c.name: c for c in caches}
    assert "jetbrains-config" in by_name
    assert by_name["jetbrains-config"].host_subpath == "jetbrains-config"
    assert by_name["jetbrains-config"].container_path == "~/.config/JetBrains"
    assert "jetbrains-data" in by_name
    assert by_name["jetbrains-data"].host_subpath == "jetbrains-data"
    assert by_name["jetbrains-data"].container_path == "~/.local/share/JetBrains"


def test_effective_shared_caches_excludes_jetbrains_when_disabled(tmp_path):
    cfg = make_cfg(tmp_path, jetbrains={"enabled": False}, shared_caches=[])
    names = [c.name for c in cfg.effective_shared_caches()]
    assert "jetbrains-config" not in names
    assert "jetbrains-data" not in names


def test_effective_shared_caches_manual_jetbrains_entry_suppresses_auto_add(tmp_path):
    """User-supplied `shared_caches` entry named `jetbrains-config` wins;
    the auto-add is silently skipped (same precedent as the claude pattern
    and as effective_host_mounts)."""
    cfg = make_cfg(
        tmp_path,
        jetbrains={"enabled": True},
        shared_caches=[
            {
                "name": "jetbrains-config",
                "host_subpath": "custom-jb-config",
                "container_path": "~/.config/JetBrains",
            },
        ],
    )
    caches = cfg.effective_shared_caches()
    entries = [c for c in caches if c.name == "jetbrains-config"]
    assert len(entries) == 1
    assert entries[0].host_subpath == "custom-jb-config"
    # The other auto-add (`jetbrains-data`) is still appended because no
    # manual entry with that name was supplied.
    assert any(c.name == "jetbrains-data" for c in caches)


def test_effective_shared_caches_includes_jetbrains_idea_when_share_idea_on(tmp_path):
    """share_idea defaults to True; when jetbrains.enabled is also True the
    cache appears with container_path derived from container_prefix."""
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True}, shared_caches=[])
    caches = cfg.effective_shared_caches()
    by_name = {c.name: c for c in caches}
    assert "jetbrains-idea" in by_name
    assert by_name["jetbrains-idea"].host_subpath == "jetbrains-idea"
    assert by_name["jetbrains-idea"].container_path == f"~/{cfg.container_prefix}/.idea"


def test_effective_shared_caches_excludes_jetbrains_idea_when_share_idea_off(tmp_path):
    """share_idea=False suppresses the auto-add even with jetbrains.enabled."""
    cfg = make_cfg(
        tmp_path,
        jetbrains={"enabled": True, "share_idea": False},
        shared_caches=[],
    )
    names = [c.name for c in cfg.effective_shared_caches()]
    assert "jetbrains-idea" not in names
    # Other jetbrains entries still present.
    assert "jetbrains-config" in names
    assert "jetbrains-data" in names


def test_effective_shared_caches_excludes_jetbrains_idea_when_jetbrains_disabled(tmp_path):
    """jetbrains.enabled=False suppresses everything regardless of share_idea."""
    cfg = make_cfg(
        tmp_path,
        jetbrains={"enabled": False, "share_idea": True},
        shared_caches=[],
    )
    names = [c.name for c in cfg.effective_shared_caches()]
    assert "jetbrains-idea" not in names


def test_effective_shared_caches_jetbrains_idea_uses_container_prefix(tmp_path):
    """container_prefix override drives the container_path, not repo_root.name."""
    repo = tmp_path / "SampleApp"
    repo.mkdir()
    cfg = make_cfg(
        repo,
        container_prefix="gisgro",
        jetbrains={"enabled": True},
        shared_caches=[],
    )
    caches = cfg.effective_shared_caches()
    entry = next(c for c in caches if c.name == "jetbrains-idea")
    assert entry.container_path == "~/gisgro/.idea"


def test_effective_shared_caches_manual_jetbrains_idea_entry_suppresses_auto_add(tmp_path):
    """User-supplied `shared_caches` entry named `jetbrains-idea` wins."""
    cfg = make_cfg(
        tmp_path,
        jetbrains={"enabled": True},
        shared_caches=[
            {
                "name": "jetbrains-idea",
                "host_subpath": "custom-idea",
                "container_path": "~/custom/.idea",
            },
        ],
    )
    caches = cfg.effective_shared_caches()
    entries = [c for c in caches if c.name == "jetbrains-idea"]
    assert len(entries) == 1
    assert entries[0].host_subpath == "custom-idea"
    assert entries[0].container_path == "~/custom/.idea"
