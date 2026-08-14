"""Tests for `gie doctor`."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jailbee.config import load_config
from jailbee.doctor import run_checks
from jailbee.registry import MirrorStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def incus_on_path():
    """Default every doctor test to a host that has the `incus` binary.

    doctor gates its Incus-dependent checks on `shutil.which("incus")`, so
    without this the suite's verdict would depend on whether the machine
    running it happens to have Incus installed — a real host dependency in
    an otherwise fully-mocked suite. Tests about a *missing* binary, or
    about `docker` being present, patch `which` themselves and win: their
    patch is applied after this one.
    """

    def which_stub(name):
        return "/usr/bin/incus" if name == "incus" else None

    with patch("jailbee.doctor.shutil.which", side_effect=which_stub):
        yield


def _cfg(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    return cfg.model_copy(update={"shared_dir": tmp_path / "shared"})


def _baseline_incus():
    """Return a MagicMock that makes every other doctor check pass.

    The bridge-check tests only care about `network_exists`; the other
    checks must not flip the assertion target by accident.
    """
    incus = MagicMock()
    incus.profile_exists.return_value = True
    incus.network_acl_exists.return_value = True
    return incus


def test_doctor_reports_loose_bridge_present_when_exists(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    bridge = [r for r in results if r.name == "network jailbee-loose"]
    assert len(bridge) == 1
    assert bridge[0].ok is True
    assert bridge[0].detail == "present"


def test_doctor_reports_loose_bridge_missing_when_absent(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = False

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    bridge = [r for r in results if r.name == "network jailbee-loose"]
    assert len(bridge) == 1
    assert bridge[0].ok is False
    assert "missing" in bridge[0].detail
    assert "jailbee init" in bridge[0].detail


def test_doctor_reports_registry_running(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    mirror = next(r for r in results if r.name == "registry mirror")
    assert mirror.ok is True
    assert "running" in mirror.detail


def test_doctor_reports_registry_degraded(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.DEGRADED):
        results = run_checks(cfg, incus)

    mirror = next(r for r in results if r.name == "registry mirror")
    assert mirror.ok is False
    assert "degraded" in mirror.detail.lower()
    # Inline remediation hint per the spec
    assert "jailbee registry up" in mirror.detail


def test_doctor_reports_registry_stopped(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.STOPPED):
        results = run_checks(cfg, incus)

    mirror = next(r for r in results if r.name == "registry mirror")
    assert mirror.ok is False
    assert "stopped" in mirror.detail
    assert "jailbee registry up" in mirror.detail


def test_doctor_reports_registry_missing(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.MISSING):
        results = run_checks(cfg, incus)

    mirror = next(r for r in results if r.name == "registry mirror")
    assert mirror.ok is False
    assert "missing" in mirror.detail
    assert "jailbee registry up" in mirror.detail


def test_doctor_warns_on_legacy_host_docker_mirror(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    # Simulate `docker` installed + the old mirror container present.
    def which_stub(name):
        return f"/usr/bin/{name}" if name in ("incus", "docker") else None

    with (
        patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING),
        patch("jailbee.doctor.shutil.which", side_effect=which_stub),
        patch(
            "jailbee.doctor.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="jailbee-registry-mirror\n", stderr=""),
        ),
    ):
        results = run_checks(cfg, incus)

    legacy = next((r for r in results if r.name == "legacy host-Docker mirror"), None)
    assert legacy is not None
    assert legacy.ok is False
    assert "docker rm" in legacy.detail


def test_doctor_skips_legacy_check_when_docker_absent(tmp_path):
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    def which_stub(name):
        return "/usr/bin/incus" if name == "incus" else None

    with (
        patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING),
        patch("jailbee.doctor.shutil.which", side_effect=which_stub),
    ):
        results = run_checks(cfg, incus)

    legacy = [r for r in results if r.name == "legacy host-Docker mirror"]
    assert legacy == []


def test_doctor_skips_gpg_check_when_gpg_disabled(tmp_path):
    cfg = _cfg(tmp_path).model_copy(
        update={
            "gpg": load_config(FIXTURES / "full_config.yaml").gpg.model_copy(
                update={"enabled": False}
            )
        }
    )
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    gpg_results = [r for r in results if "gpg-agent" in r.name.lower()]
    assert gpg_results == []


def test_doctor_runs_gpg_check_when_gpg_enabled(tmp_path):
    cfg = _cfg(tmp_path)  # default gpg.enabled = True
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    gpg_results = [r for r in results if "gpg-agent" in r.name.lower()]
    assert len(gpg_results) == 1


def test_run_checks_notes_missing_git_repo(make_cfg, tmp_path):
    """When repo_root has no .git, doctor emits an info note pointing to --mount."""
    repo = tmp_path / "plain"
    repo.mkdir()  # no .git inside
    cfg = make_cfg(repo)
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    git_checks = [r for r in results if r.name == "host git repo"]
    assert len(git_checks) == 1
    assert git_checks[0].ok is True
    assert "--mount" in git_checks[0].detail


def test_run_checks_omits_git_note_when_repo_is_git(make_cfg, tmp_path):
    """When repo_root has .git, no `host git repo` note is emitted."""
    repo = tmp_path / "with-git"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = make_cfg(repo)
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    assert not any(r.name == "host git repo" for r in results)


def test_doctor_does_not_flag_missing_claude_when_disabled(tmp_path):
    """When claude.enabled is false, doctor does not list claude as a
    missing subdir even if <shared_dir>/claude does not exist."""
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"claude": cfg.claude.model_copy(update={"enabled": False})})
    # Create only the non-claude expected subdirs.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "jetbrains-config",
        "jetbrains-idea",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is True, tree.detail
    # When claude is disabled it must not be reported missing. Assert on
    # the missing-list semantics (the check passed, so nothing is missing)
    # rather than a substring of the success detail — that detail embeds
    # <shared_dir>, whose path may legitimately contain the word "claude"
    # (e.g. a checkout under /tmp/claude-*). The old `"claude" not in
    # tree.detail` false-failed in exactly that case.
    assert "missing" not in tree.detail


def test_doctor_flags_missing_claude_when_enabled(tmp_path):
    """When claude.enabled is true, doctor flags missing <shared_dir>/claude."""
    cfg = _cfg(tmp_path)  # fixture has claude.enabled: true after Task 3
    # Create only the non-claude expected subdirs.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "jetbrains-config",
        "jetbrains-idea",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is False
    assert "claude" in tree.detail


def test_doctor_flags_missing_claude_install_when_enabled(tmp_path):
    """claude.enabled and <shared_dir>/claude-install absent → flagged."""
    cfg = _cfg(tmp_path)  # fixture has claude.enabled: true
    # Create every expected subdir EXCEPT claude-install.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "jetbrains-config",
        "jetbrains-idea",
        "claude",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is False
    assert "claude-install" in tree.detail


def test_doctor_does_not_flag_missing_jetbrains_when_disabled(tmp_path):
    """When jetbrains.enabled is false, doctor does not list jetbrains-config
    as a missing subdir even if <shared_dir>/jetbrains-config does not exist."""
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": False})})
    # Create only the non-jetbrains expected subdirs (+ claude and claude-install,
    # which the fixture has enabled, so doctor expects them too).
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "claude",
        "claude-install",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is True, tree.detail
    assert "jetbrains-config" not in tree.detail


def test_doctor_flags_missing_jetbrains_when_enabled(tmp_path):
    """When jetbrains.enabled is true, doctor flags missing
    <shared_dir>/jetbrains-config."""
    cfg = _cfg(tmp_path)  # fixture has jetbrains.enabled: true
    # Create only the non-jetbrains expected subdirs.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "claude",
        "claude-install",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is False
    assert "jetbrains-config" in tree.detail


def test_doctor_flags_missing_jetbrains_idea_when_share_idea_on(tmp_path):
    """jetbrains.enabled + share_idea (default) → doctor expects
    <shared_dir>/jetbrains-idea to exist."""
    cfg = _cfg(tmp_path)  # fixture has jetbrains.enabled: true; share_idea defaults True
    # Create the other expected subdirs but NOT jetbrains-idea.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "jetbrains-config",
        "claude",
        "claude-install",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is False
    assert "jetbrains-idea" in tree.detail


def test_doctor_does_not_flag_missing_jetbrains_idea_when_share_idea_off(tmp_path):
    """share_idea=False → doctor does not expect the .idea subdir."""
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(
        update={"jetbrains": cfg.jetbrains.model_copy(update={"share_idea": False})}
    )
    # Everything except jetbrains-idea.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "jetbrains-config",
        "claude",
        "claude-install",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is True, tree.detail
    assert "jetbrains-idea" not in tree.detail


def test_doctor_does_not_flag_missing_jetbrains_idea_when_jetbrains_disabled(tmp_path):
    """jetbrains.enabled=False → doctor does not expect any jetbrains subdir."""
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": False})})
    # Skip every jetbrains subdir.
    for sub in (
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
        "claude",
        "claude-install",
    ):
        (tmp_path / "shared" / sub).mkdir(parents=True, exist_ok=True)
    incus = _baseline_incus()
    incus.network_exists.return_value = True

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    tree = next(r for r in results if r.name == "shared_dir tree")
    assert tree.ok is True, tree.detail
    assert "jetbrains-idea" not in tree.detail


# ---------- github integration


def test_check_github_skipped_when_disabled(tmp_path):
    from jailbee.doctor import _check_github
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, github={"enabled": False})
    assert _check_github(cfg) == []


def test_check_github_info_when_prefix_absent_from_map(tmp_path):
    from jailbee.doctor import _check_github
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="some-other-repo",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
    )
    results = _check_github(cfg)
    assert len(results) == 1
    assert results[0].ok is True
    assert "no token configured" in results[0].detail
    assert "github_pat_xxx" not in results[0].detail


def _set_global_yaml_perms(monkeypatch, tmp_path, mode: int) -> Path:
    fake_home = tmp_path / "fakehome"
    (fake_home / ".config" / "jailbee").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    gy = fake_home / ".config" / "jailbee" / "global.yaml"
    gy.write_text("# placeholder")
    gy.chmod(mode)
    return gy


def test_check_github_fine_grained_token_ok(tmp_path, monkeypatch):
    from jailbee.doctor import _check_github
    from tests.conftest import make_cfg

    _set_global_yaml_perms(monkeypatch, tmp_path, 0o600)
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={
            "enabled": True,
            "api_tokens": {"sampleapp": "github_pat_xxx"},
        },
    )
    results = _check_github(cfg)
    names = {r.name: r for r in results}
    assert names["github global.yaml perms"].ok is True
    assert names["github global.yaml perms"].detail == "0600"
    assert names["github token non-empty"].ok is True
    assert names["github token shape"].ok is True
    assert names["github token shape"].detail == "fine-grained PAT"


def test_check_github_classic_pat_warns(tmp_path, monkeypatch):
    from jailbee.doctor import _check_github
    from tests.conftest import make_cfg

    _set_global_yaml_perms(monkeypatch, tmp_path, 0o600)
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={
            "enabled": True,
            "api_tokens": {"sampleapp": "ghp_classic_token_value"},
        },
    )
    results = _check_github(cfg)
    shape = next(r for r in results if r.name == "github token shape")
    assert shape.ok is False
    assert "classic PAT" in shape.detail
    assert "ghp_classic_token_value" not in shape.detail


def test_check_github_unknown_shape_warns(tmp_path, monkeypatch):
    from jailbee.doctor import _check_github
    from tests.conftest import make_cfg

    _set_global_yaml_perms(monkeypatch, tmp_path, 0o600)
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={
            "enabled": True,
            "api_tokens": {"sampleapp": "weird_prefix_token"},
        },
    )
    results = _check_github(cfg)
    shape = next(r for r in results if r.name == "github token shape")
    assert shape.ok is False
    assert "unknown token format" in shape.detail
    assert "weird_prefix_token" not in shape.detail


def test_check_github_insecure_global_perms(tmp_path, monkeypatch):
    from jailbee.doctor import _check_github
    from tests.conftest import make_cfg

    _set_global_yaml_perms(monkeypatch, tmp_path, 0o644)
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={
            "enabled": True,
            "api_tokens": {"sampleapp": "github_pat_xxx"},
        },
    )
    results = _check_github(cfg)
    perms = next(r for r in results if r.name == "github global.yaml perms")
    assert perms.ok is False
    assert "insecure perms" in perms.detail


# ---- egress pool checks ----


def test_doctor_pool_check_timer_inactive(tmp_path: Path, mocker) -> None:
    from jailbee.doctor import _check_egress_pool

    cfg = _cfg(tmp_path)
    mocker.patch(
        "jailbee.doctor.subprocess.run",
        return_value=mocker.Mock(stdout="inactive\n", returncode=3),
    )
    fake_session = mocker.MagicMock()
    fake_session.__enter__.return_value = fake_session
    fake_session.get.return_value = None
    fake_session.exec.return_value.all.return_value = []
    mocker.patch("jailbee.doctor.Session", return_value=fake_session)
    mocker.patch("jailbee.doctor.get_engine")

    results = _check_egress_pool(cfg)
    timer_check = next(r for r in results if "timer" in r.name)
    assert timer_check.ok is False
    assert "inactive" in timer_check.detail


# ---- kernel keyring quota check ----


def _write_keyring_proc(
    tmp_path: Path,
    maxkeys: int,
    uid_line: str | None,
) -> tuple[Path, Path]:
    """Fabricate /proc/sys/kernel/keys/maxkeys and /proc/key-users files."""
    mk = tmp_path / "maxkeys"
    mk.write_text(f"{maxkeys}\n")
    ku = tmp_path / "key-users"
    body = "    0:   773 772/772 253/1000000 5610/25000000\n"
    if uid_line is not None:
        body += uid_line + "\n"
    ku.write_text(body)
    return mk, ku


def test_keyring_check_returns_empty_when_proc_missing(tmp_path: Path) -> None:
    from jailbee.doctor import _check_kernel_keyring

    results = _check_kernel_keyring(
        maxkeys_path=tmp_path / "nope_maxkeys",
        key_users_path=tmp_path / "nope_key_users",
    )
    assert results == []


def test_keyring_check_flags_low_maxkeys(tmp_path: Path) -> None:
    from jailbee.doctor import _check_kernel_keyring

    mk, ku = _write_keyring_proc(
        tmp_path,
        maxkeys=200,
        uid_line="1000000:    50 50/50 50/200 500/20000",
    )
    results = _check_kernel_keyring(maxkeys_path=mk, key_users_path=ku)
    assert len(results) == 1
    assert results[0].ok is False
    assert "maxkeys=200" in results[0].detail
    assert "disk quota exceeded" in results[0].detail
    assert "/etc/sysctl.d/99-jailbee-keys.conf" in results[0].detail
    assert "kernel.keys.maxkeys=2000" in results[0].detail


def test_keyring_check_flags_high_uid_usage(tmp_path: Path) -> None:
    """maxkeys is fine, but uid 1000000 already at 199/200 → warn."""
    from jailbee.doctor import _check_kernel_keyring

    mk, ku = _write_keyring_proc(
        tmp_path,
        maxkeys=2000,
        uid_line="1000000:   199 199/199 199/200 6151/20000",
    )
    results = _check_kernel_keyring(maxkeys_path=mk, key_users_path=ku)
    assert len(results) == 1
    assert results[0].ok is False
    assert "199/200" in results[0].detail
    assert "1000000" in results[0].detail
    assert "/etc/sysctl.d/99-jailbee-keys.conf" in results[0].detail


def test_keyring_check_passes_when_usage_low(tmp_path: Path) -> None:
    from jailbee.doctor import _check_kernel_keyring

    mk, ku = _write_keyring_proc(
        tmp_path,
        maxkeys=2000,
        uid_line="1000000:    30 30/30 30/2000 500/200000",
    )
    results = _check_kernel_keyring(maxkeys_path=mk, key_users_path=ku)
    assert len(results) == 1
    assert results[0].ok is True
    assert "30/2000" in results[0].detail


def test_keyring_check_passes_when_no_mapped_root_entry(tmp_path: Path) -> None:
    """No uid 1000000 line (no containers started yet) is fine if maxkeys is high enough."""
    from jailbee.doctor import _check_kernel_keyring

    mk, ku = _write_keyring_proc(tmp_path, maxkeys=2000, uid_line=None)
    results = _check_kernel_keyring(maxkeys_path=mk, key_users_path=ku)
    assert len(results) == 1
    assert results[0].ok is True
    assert "no uid 1000000 entry" in results[0].detail


def test_keyring_check_tolerates_malformed_lines(tmp_path: Path) -> None:
    """Garbage lines in /proc/key-users do not crash the parser."""
    from jailbee.doctor import _check_kernel_keyring

    mk = tmp_path / "maxkeys"
    mk.write_text("2000\n")
    ku = tmp_path / "key-users"
    ku.write_text(
        "garbage line without colon\n"
        "notanumber:  1 1/1 1/200 9/20000\n"
        "1000000:   50 50/50 50/2000 500/200000\n"
    )
    results = _check_kernel_keyring(maxkeys_path=mk, key_users_path=ku)
    assert len(results) == 1
    assert results[0].ok is True
    assert "50/2000" in results[0].detail


def test_keyring_check_handles_unparseable_maxkeys(tmp_path: Path) -> None:
    """A corrupt /proc/sys/kernel/keys/maxkeys is silently skipped."""
    from jailbee.doctor import _check_kernel_keyring

    mk = tmp_path / "maxkeys"
    mk.write_text("not a number\n")
    ku = tmp_path / "key-users"
    ku.write_text("1000000:   50 50/50 50/2000 500/200000\n")
    results = _check_kernel_keyring(maxkeys_path=mk, key_users_path=ku)
    assert results == []


def test_run_checks_includes_keyring_when_proc_available(tmp_path: Path, monkeypatch) -> None:
    """run_checks wires _check_kernel_keyring in and reports its result."""
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    mk, ku = _write_keyring_proc(
        tmp_path,
        maxkeys=2000,
        uid_line="1000000:    30 30/30 30/2000 500/200000",
    )
    monkeypatch.setattr("jailbee.doctor._KERNEL_KEYS_MAXKEYS", mk)
    monkeypatch.setattr("jailbee.doctor._KERNEL_KEY_USERS", ku)

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    keyring = [r for r in results if r.name == "kernel keyring quota"]
    assert len(keyring) == 1
    assert keyring[0].ok is True


def test_run_checks_omits_keyring_when_proc_unavailable(tmp_path: Path, monkeypatch) -> None:
    """When /proc keyring files are missing, the check is silently skipped."""
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    monkeypatch.setattr("jailbee.doctor._KERNEL_KEYS_MAXKEYS", tmp_path / "missing_mk")
    monkeypatch.setattr("jailbee.doctor._KERNEL_KEY_USERS", tmp_path / "missing_ku")

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    assert not any(r.name == "kernel keyring quota" for r in results)


def test_doctor_pool_check_recent_refresh_ok(tmp_path: Path, mocker) -> None:
    from datetime import UTC, datetime, timedelta

    from jailbee.db.models import PoolIP, RefreshState
    from jailbee.doctor import _check_egress_pool

    cfg = _cfg(tmp_path)
    mocker.patch(
        "jailbee.doctor.subprocess.run",
        return_value=mocker.Mock(stdout="active\n", returncode=0),
    )

    now = datetime.now(UTC)
    fake_session = mocker.MagicMock()
    fake_session.__enter__.return_value = fake_session
    fake_session.get.return_value = RefreshState(
        container_prefix=cfg.container_prefix,
        last_refresh_at=now - timedelta(seconds=30),
        last_refresh_status="ok",
    )
    fake_session.exec.return_value.all.return_value = [
        PoolIP(
            container_prefix=cfg.container_prefix,
            hostname="github.com",
            ip="1.1.1.1",
            first_seen=now,
            last_seen=now,
        ),
    ]
    mocker.patch("jailbee.doctor.Session", return_value=fake_session)
    mocker.patch("jailbee.doctor.get_engine")

    results = _check_egress_pool(cfg)
    refresh_check = next(r for r in results if "last refresh" in r.name)
    assert refresh_check.ok is True


# ---- pre-1.0 gie state checks ----


def test_doctor_flags_leftover_pre_1_0_state(tmp_path, mocker, make_cfg):
    from jailbee.doctor import run_checks

    mocker.patch(
        "jailbee.migrate.leftovers",
        return_value=("old labels on container app-feat",),
    )
    results = run_checks(make_cfg(tmp_path / "repo"), mocker.MagicMock())

    check = next(r for r in results if r.name == "pre-1.0 gie state")
    assert check.ok is False
    assert "jailbee migrate" in check.detail
    # Name what was found, not just that something was: the migrator skips
    # some of it, so "run migrate" alone can be advice that changes nothing.
    assert "old labels on container app-feat" in check.detail
    # The guide is the only place the manual steps are written down.
    assert "docs/migrating-from-gie.md" in check.detail


def test_doctor_is_happy_when_no_pre_1_0_state_remains(tmp_path, mocker, make_cfg):
    from jailbee.doctor import run_checks

    mocker.patch("jailbee.migrate.leftovers", return_value=())
    results = run_checks(make_cfg(tmp_path / "repo"), mocker.MagicMock())

    check = next(r for r in results if r.name == "pre-1.0 gie state")
    assert check.ok is True


def test_doctor_reports_state_no_migration_plan_would_show(tmp_path, mocker, make_cfg):
    """The check must not be derived from `migrate.build_plan`.

    A directory whose target already exists is a blocker, not a planned
    move, so `plan.is_empty` is True while the state is very much still
    there — the case that silently loses a user's whole state directory.
    """
    from jailbee.doctor import run_checks

    build_plan = mocker.patch("jailbee.migrate.build_plan")
    mocker.patch("jailbee.migrate.leftovers", return_value=("directory /home/u/.local/state/gie",))

    results = run_checks(make_cfg(tmp_path / "repo"), mocker.MagicMock())

    check = next(r for r in results if r.name == "pre-1.0 gie state")
    assert check.ok is False
    assert "directory /home/u/.local/state/gie" in check.detail
    build_plan.assert_not_called()


def test_doctor_flags_legacy_config_in_repo(tmp_path, mocker, make_cfg):
    from jailbee.doctor import run_checks

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gie").mkdir()
    (repo / ".gie" / "config.yaml").write_text("# legacy config\n")

    mocker.patch("jailbee.migrate.leftovers", return_value=())
    results = run_checks(make_cfg(repo), mocker.MagicMock())

    check = next(r for r in results if r.name == "pre-1.0 gie state")
    assert check.ok is False
    assert "jailbee migrate" in check.detail
    assert "git mv .gie .jailbee" in check.detail


# ---- graphical session: Wayland is the only session GUI passthrough works on ----


def _graphical_session(results):
    return next(r for r in results if r.name == "graphical session")


def test_doctor_passes_the_graphical_check_on_wayland(tmp_path, monkeypatch, make_cfg, mocker):
    from jailbee.doctor import run_checks

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")  # a Wayland session may set both
    mocker.patch("jailbee.migrate.leftovers", return_value=())

    check = _graphical_session(run_checks(make_cfg(tmp_path / "repo"), mocker.MagicMock()))
    assert check.ok is True
    assert check.detail == "Wayland"


def test_doctor_fails_the_graphical_check_on_a_bare_x11_session(
    tmp_path, monkeypatch, make_cfg, mocker
):
    """The regression this guards: an X11 session used to report a green
    'graphical session: X11'. Only the compositor's own socket is passed
    into containers, so `jailbee ide` starts and never opens a window —
    the check must say so instead of implying it will work."""
    from jailbee.doctor import run_checks

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    mocker.patch("jailbee.migrate.leftovers", return_value=())

    check = _graphical_session(run_checks(make_cfg(tmp_path / "repo"), mocker.MagicMock()))
    assert check.ok is False
    assert "X11" in check.detail
    assert "will not display" in check.detail


def test_doctor_fails_the_graphical_check_with_no_session_at_all(
    tmp_path, monkeypatch, make_cfg, mocker
):
    from jailbee.doctor import run_checks

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    mocker.patch("jailbee.migrate.leftovers", return_value=())

    check = _graphical_session(run_checks(make_cfg(tmp_path / "repo"), mocker.MagicMock()))
    assert check.ok is False
    assert "WAYLAND_DISPLAY" in check.detail


# ---- loose bridge: present is not the same as carrying traffic ----


def _bridge_check(results):
    return next((r for r in results if r.name.endswith("addressing")), None)


def _running(name, profiles, ipv4=None):
    addresses = [{"family": "inet", "scope": "global", "address": ipv4}] if ipv4 else []
    return {
        "name": name,
        "status": "Running",
        "profiles": profiles,
        "state": {"network": {"eth0": {"addresses": addresses}}},
    }


def test_doctor_flags_a_loose_bridge_that_hands_out_no_addresses(tmp_path, make_cfg, mocker):
    """The gap this closes: `doctor` called the bridge "present" while a host
    firewall dropped DHCP to it, so containers had IPv6 and no IPv4 and the
    only visible symptom was apt failing to resolve, ten minutes later."""
    from jailbee.doctor import run_checks

    cfg = make_cfg(tmp_path / "repo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _running("jailbee-registry-mirror", ["default", "jailbee-registry-mirror-profile"]),
        _running("app-feat", [f"{cfg.container_prefix}-net-loose"]),
    ]
    mocker.patch("jailbee.migrate.leftovers", return_value=())

    check = _bridge_check(run_checks(cfg, incus))

    assert check is not None
    assert check.ok is False
    assert "jailbee-registry-mirror" in check.detail and "app-feat" in check.detail
    assert "firewall" in check.detail


def test_doctor_passes_when_the_loose_bridge_addresses_a_container(tmp_path, make_cfg, mocker):
    from jailbee.doctor import run_checks

    cfg = make_cfg(tmp_path / "repo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _running("jailbee-registry-mirror", ["default"], ipv4="10.165.192.2"),
    ]
    mocker.patch("jailbee.migrate.leftovers", return_value=())

    check = _bridge_check(run_checks(cfg, incus))

    assert check is not None
    assert check.ok is True


def test_doctor_stays_silent_when_nothing_runs_on_the_loose_bridge(tmp_path, make_cfg, mocker):
    """No evidence must not become a verdict: an empty bridge is the normal
    state on a host that has not switched anything to loose mode."""
    from jailbee.doctor import run_checks

    cfg = make_cfg(tmp_path / "repo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "app-feat",
            "status": "Running",
            "profiles": [f"{cfg.container_prefix}-net-strict"],
        },
        {"name": "app-old", "status": "Stopped", "profiles": [f"{cfg.container_prefix}-net-loose"]},
    ]
    mocker.patch("jailbee.migrate.leftovers", return_value=())

    assert _bridge_check(run_checks(cfg, incus)) is None


def test_run_checks_reports_the_resolved_upstream_remote(make_cfg, tmp_path):
    """Detection is invisible unless doctor says what it landed on."""
    repo = tmp_path / "with-git"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = make_cfg(repo, upstream_remote="public", default_branch="dev")
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    with (
        patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING),
        patch("jailbee.doctor.detect_upstream_remote", return_value="public"),
    ):
        results = run_checks(cfg, incus)

    checks = [r for r in results if r.name == "upstream remote"]
    assert len(checks) == 1
    assert checks[0].ok is True
    assert "public" in checks[0].detail
    assert "dev" in checks[0].detail


def test_run_checks_flags_an_unresolvable_upstream_remote(make_cfg, tmp_path):
    """Several remotes and no signal picking one: jailbee falls back to the
    literal `origin`, which is a guess, so say so rather than look healthy."""
    repo = tmp_path / "with-git"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = make_cfg(repo)
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    with (
        patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING),
        patch("jailbee.doctor.detect_upstream_remote", return_value=None),
    ):
        results = run_checks(cfg, incus)

    checks = [r for r in results if r.name == "upstream remote"]
    assert len(checks) == 1
    assert checks[0].ok is False
    assert "remote.pushDefault" in checks[0].detail


def test_run_checks_skips_the_upstream_remote_check_without_a_git_repo(make_cfg, tmp_path):
    """Nothing to resolve, and the missing-.git note already covers it."""
    repo = tmp_path / "plain"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = _baseline_incus()
    incus.network_exists.return_value = True
    incus.list_containers.return_value = []

    with patch("jailbee.doctor.registry_status", return_value=MirrorStatus.RUNNING):
        results = run_checks(cfg, incus)

    assert not any(r.name == "upstream remote" for r in results)


def test_doctor_reports_missing_incus_binary_instead_of_crashing(tmp_path):
    """No `incus` on PATH is the state doctor exists to report, not to die on.

    Before, the binary check recorded a failure and then the very next check
    called through to the CLI anyway, so `jailbee doctor` on a host that had
    not installed Incus yet ended in a raw FileNotFoundError traceback — the
    recorded failure never reached the user.
    """
    cfg = _cfg(tmp_path)
    incus = _baseline_incus()

    with patch("jailbee.doctor.shutil.which", return_value=None):
        results = run_checks(cfg, incus)

    binary = next(r for r in results if r.name == "incus binary")
    assert binary.ok is False
    assert "PATH" in binary.detail

    # The dependent checks did not merely fail — they never touched Incus.
    incus.profile_exists.assert_not_called()
    incus.network_acl_exists.assert_not_called()
    incus.network_exists.assert_not_called()
    incus.list_containers.assert_not_called()
    assert [r for r in results if r.name.startswith("profile ")] == []

    # ...and the report says so once, not once per check it could not run.
    skipped = [r for r in results if "skipped" in r.detail]
    assert len(skipped) == 1

    # Host-side checks that need no daemon still run, so one missing
    # prerequisite does not blank out the rest of the diagnosis.
    assert any(r.name == "shared_dir tree" for r in results)
    assert any(r.name == "container_user uid/gid" for r in results)
