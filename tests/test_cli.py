"""Smoke tests for CLI invocation."""

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config import load_config
from jailbee.lifecycle import ResolvedContainer
from tests.conftest import make_config

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _fake_pool(name: str):
    from jailbee.config import PoolSpec
    from jailbee.pool import Pool

    return Pool(name=name, root=Path("/tmp/x"), container_path="/home/dev/x", spec=PoolSpec())


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Drive the real console script in a subprocess.

    Spawns `jailbee`, the primary of the two entry points `pyproject.toml`
    declares. This is the only place the suite depends on a script name being
    installed, which is what caught the removal of the pre-1.0 `gie` alias.
    """
    return subprocess.run(
        ["uv", "run", "jailbee", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent.parent,
    )


def test_version() -> None:
    from jailbee import __version__

    result = run_cli("version")
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_version_flag() -> None:
    """`--version` is what users reach for, and the docs promise it works."""
    from jailbee import __version__

    result = run_cli("--version")
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_help() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    assert "jailbee" in result.stdout.lower()


def test_cli_init_no_longer_has_reapply_flag() -> None:
    """`--reapply` was removed — `gie apply` is the new entry point."""
    result = CliRunner().invoke(app, ["init", "--reapply"])
    assert result.exit_code != 0


def test_config_show_with_explicit_path() -> None:
    result = run_cli("config", "show", "--config", str(FIXTURES / "minimal_config.yaml"))
    assert result.returncode == 0
    assert "container_user" in result.stdout


def test_config_validate_failure_for_missing_repo() -> None:
    result = run_cli("config", "validate", "--config", str(FIXTURES / "minimal_config.yaml"))
    # exit 2 = runtime issues; exit 0 if /tmp/test-repo happens to exist
    assert result.returncode in (0, 2)


def test_config_validate_fails_on_a_global_column_typo(tmp_path, monkeypatch) -> None:
    """Unlike ordinary commands (which now recover from this — see
    `test_ls_warns_but_still_runs_on_a_global_column_typo`), `gie config
    validate` is the one place a typo in `global.yaml`'s column blocks must
    still fail, with the allowed names listed. It used to be invisible here
    entirely: this command never called `_load_global()`."""
    _write_global_columns(tmp_path, monkeypatch, "ls:\n  fields: [name, nosuchfield]\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 2
    assert "nosuchfield" in result.stdout
    assert "allowed:" in result.stdout


def test_config_validate_fails_on_a_host_level_global_error(tmp_path, monkeypatch) -> None:
    """A genuine host-level schema problem (not a column typo) stays fatal
    even here."""
    _write_global_columns(tmp_path, monkeypatch, "docker_registry_mirror:\n  port: not-a-number\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 1


def test_config_validate_passes_with_a_valid_global_column_block(tmp_path, monkeypatch) -> None:
    _write_global_columns(tmp_path, monkeypatch, "ls:\n  fields: [name, state]\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 0, result.stdout


def test_config_validate_keeps_bracketed_text_in_a_validator_message(tmp_path) -> None:
    """A validation message's square brackets must survive to the terminal.

    `host_ports`'s name rule quotes its regex, `[a-z0-9][a-z0-9-]*`. Printed
    through `error` (Rich markup on), Rich reads each bracket group as a style
    tag and silently deletes it, so the user is told the name "must match *" —
    the rule the message exists to state, gone. `error_plain` is what keeps it.
    """
    repo = _setup_repo_with_columns(tmp_path, "host_ports:\n  - name: ADB\n    port: 5037\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 1
    # stderr, not stdout: `error`/`error_plain` print to `tui.err_console`.
    assert "[a-z0-9][a-z0-9-]*" in result.stderr


def test_config_validate_fails_on_a_repo_column_typo(tmp_path) -> None:
    """The repo-layer mirror of `test_config_validate_fails_on_a_global_column_typo`:
    ordinary loading (`load_config`) now recovers from a typo in a repo's
    own `ls:`/`dashboard:` blocks (see the `test_ls_*_repo_column_*` tests
    below) — but `gie config validate` must still fail on it, with the
    allowed names listed, exactly as it always has. It calls
    `load_config_unsanitized` (not `load_config`) specifically so this stays
    true."""
    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, nosuchfield]\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 2
    assert "nosuchfield" in result.stdout
    assert "allowed:" in result.stdout


def test_config_validate_fails_on_a_repo_empty_fields_list(tmp_path) -> None:
    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: []\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 2
    assert "fields is empty" in result.stdout


def test_config_validate_fails_on_a_repo_duplicated_field(tmp_path) -> None:
    repo = _setup_repo_with_columns(tmp_path, "dashboard:\n  fields: [name, state, name]\n")

    result = CliRunner().invoke(
        app, ["config", "validate", "--config", str(repo / ".jailbee" / "config.yaml")]
    )

    assert result.exit_code == 2
    assert "duplicate field 'name'" in result.stdout


def test_ls_warns_but_still_runs_on_a_repo_column_typo(mocker, tmp_path):
    """The repo-layer mirror of `test_ls_warns_but_still_runs_on_a_global_column_typo`:
    an unknown column name in the repo's own `.jailbee/config.yaml` is a
    personal display preference too, so `gie ls` warns and proceeds with
    the remaining valid names instead of failing."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, nosuchfield]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "nosuchfield" in combined
    assert "NAME" in result.stdout


def test_ls_repo_column_warning_names_the_repo_config_file(mocker, tmp_path):
    """The warning must say which file the problem is in, now that both the
    global and repo layers can produce one — otherwise a repo-layer typo
    reads as if it came from `global.yaml`, or vice versa."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, nosuchfield]\n")
    repo_config_path = repo / ".jailbee" / "config.yaml"
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo_config_path,
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert str(repo_config_path) in combined


def test_ls_renders_default_columns_when_repo_fields_is_explicitly_empty(mocker, tmp_path):
    """The bug this fix closes: an explicit `ls: {fields: []}` in a repo's
    `.jailbee/config.yaml` must fall back to the built-in default table, not
    render a table with zero columns for everyone working in that repo."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: []\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "NAME" in result.stdout
    assert "feat-x" in result.stdout
    assert "BASE" in result.stdout and "STATE" in result.stdout


def test_shell_help() -> None:
    result = run_cli("shell", "--help")
    assert result.returncode == 0
    assert "interactive" in result.stdout.lower() or "shell" in result.stdout.lower()


def test_cli_new_help_advertises_base_positional() -> None:
    """`gie new --help` shows the optional second positional `BASE`."""
    result = CliRunner().invoke(app, ["new", "--help"])
    assert result.exit_code == 0
    # Typer renders optional positionals as `[BASE]` in usage line and lists
    # the argument in the Arguments section.
    assert "BASE" in result.output
    assert "feat/wip-bar" in result.output  # one of the docstring examples


def test_cli_new_help_shows_review_example() -> None:
    """The expanded docstring documents the review/existing-branch case."""
    result = CliRunner().invoke(app, ["new", "--help"])
    assert result.exit_code == 0
    assert "review" in result.output.lower()


def test_cli_new_help_advertises_current_flag() -> None:
    """`gie new --help` shows the --current flag with an example."""
    result = CliRunner().invoke(app, ["new", "--help"])
    assert result.exit_code == 0
    assert "--current" in result.output


def test_cli_new_errors_when_no_branch_and_no_current() -> None:
    """`gie new` with no positional and no --current is an error."""
    result = CliRunner().invoke(app, ["new"])
    assert result.exit_code != 0
    assert "BRANCH" in result.output or "--current" in result.output


def test_cli_new_errors_when_current_combined_with_base_positional() -> None:
    """`gie new feat/x feat/wip --current` is contradictory.

    With BRANCH alone, --current designates the base. Supplying an
    explicit BASE positional on top of that is ambiguous.
    """
    result = CliRunner().invoke(app, ["new", "feat/x", "feat/wip", "--current"])
    assert result.exit_code != 0
    assert "--current" in result.output


def test_cli_new_with_branch_and_current_uses_host_branch_as_base(tmp_path, mocker) -> None:
    """`gie new feat/x --current` resolves host's current branch as BASE."""
    from jailbee import cli as cli_mod
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"repo_root": tmp_path})
    (tmp_path / ".git").mkdir()
    gcfg = GlobalConfig(docker_registry_mirror=DockerRegistryMirror(enabled=False))
    mocker.patch.object(cli_mod, "_load_or_exit", return_value=cfg)
    mocker.patch.object(cli_mod, "_load_global", return_value=gcfg)
    mocker.patch("jailbee.git.get_current_branch", return_value="feat/parent")
    mocker.patch("jailbee.incus.Incus")
    new_container = mocker.patch("jailbee.lifecycle.new_container", return_value="feat-x")

    result = CliRunner().invoke(app, ["new", "feat/x", "--current", "--no-autostart"])
    assert result.exit_code == 0, result.output

    opts = new_container.call_args.args[2]
    assert opts.container_branch == "feat/x"
    assert opts.base == "feat/parent"


def test_net_help() -> None:
    result = run_cli("net", "--help")
    assert result.returncode == 0
    # both mode subcommands should appear
    assert "strict" in result.stdout
    assert "loose" in result.stdout


def test_pool_ls_lists_every_pool(tmp_path, mocker):
    """`jailbee pool ls` (no NAME) concatenates slots across every pool."""
    from jailbee.pool import SlotInfo

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch("jailbee.pool.pools_for", return_value=[_fake_pool("gradle")])
    mocker.patch(
        "jailbee.pool.list_slots",
        return_value=[
            SlotInfo(
                pool="gradle",
                name="slot-0",
                path=tmp_path / "slot-0",
                container="c1",
                warmth_mtime=None,
                size_bytes=10,
            )
        ],
    )
    mocker.patch("jailbee.pool.unique_bytes", return_value=10)
    result = runner.invoke(app, ["pool", "ls"])
    assert result.exit_code == 0
    assert "gradle" in result.stdout
    assert "slot-0" in result.stdout


def test_pool_ls_rejects_an_unknown_pool_name(tmp_path, mocker):
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch("jailbee.pool.pools_for", return_value=[])
    result = runner.invoke(app, ["pool", "ls", "nosuch"])
    assert result.exit_code == 2
    assert "nosuch" in result.output


def test_pool_ls_with_name_filters_to_that_pool(tmp_path, mocker):
    """A NAME that does match narrows to that one pool's slots via `pool.get`."""
    from jailbee.pool import SlotInfo

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch("jailbee.pool.get", return_value=_fake_pool("gradle"))
    mocker.patch(
        "jailbee.pool.list_slots",
        return_value=[
            SlotInfo(
                pool="gradle",
                name="slot-0",
                path=tmp_path / "slot-0",
                container=None,
                warmth_mtime=None,
                size_bytes=10,
            )
        ],
    )
    mocker.patch("jailbee.pool.unique_bytes", return_value=10)
    result = runner.invoke(app, ["pool", "ls", "gradle"])
    assert result.exit_code == 0, result.stdout
    assert "slot-0" in result.stdout


def test_pool_ls_format_json(tmp_path, mocker):
    import json as _json

    from jailbee.pool import SlotInfo

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch("jailbee.pool.pools_for", return_value=[_fake_pool("gradle")])
    mocker.patch(
        "jailbee.pool.list_slots",
        return_value=[
            SlotInfo(
                pool="gradle",
                name="slot-0",
                path=tmp_path / "slot-0",
                container="feat-foo",
                warmth_mtime=None,
                size_bytes=1024,
            ),
        ],
    )
    mocker.patch("jailbee.pool.unique_bytes", return_value=1024)

    result = runner.invoke(
        app,
        ["pool", "ls", "--format", "json", "--fields", "pool,slot,container,size_bytes"],
    )
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data == [
        {"pool": "gradle", "slot": "slot-0", "container": "feat-foo", "size_bytes": 1024}
    ]


def test_pool_ls_prints_total_footer(tmp_path, mocker):
    """The dedup total (`unique_bytes`, summed over selected pools) is the
    only place the real on-disk figure appears — per-slot sizes double-count
    hardlinked content."""
    from jailbee.pool import SlotInfo

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch(
        "jailbee.pool.pools_for",
        return_value=[_fake_pool("gradle"), _fake_pool("chrome-profile")],
    )
    mocker.patch(
        "jailbee.pool.list_slots",
        return_value=[
            SlotInfo(
                pool="gradle",
                name="slot-0",
                path=tmp_path / "slot-0",
                container=None,
                warmth_mtime=None,
                size_bytes=99999,
            ),
        ],
    )
    mocker.patch("jailbee.pool.unique_bytes", return_value=2048)
    result = runner.invoke(app, ["pool", "ls"])
    assert result.exit_code == 0, result.stdout
    assert "total on disk (deduplicated)" in result.stdout
    # unique_bytes mocked to 2048 per pool, 2 selected pools -> 4096 -> "4.0 KB".
    # Not 99999 (the fabricated, deliberately wrong, per-slot size_bytes) —
    # the footer must come from unique_bytes(), never from summing SlotInfo.
    assert "4.0 KB" in result.stdout


def test_disk_usage_table_includes_total_footer(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.maintenance import DiskRow

    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.maintenance.gather_disk_usage",
        return_value=[
            DiskRow(component="A", size_bytes=1024, path="/a"),
            DiskRow(component="B", size_bytes=3072, path="/b"),
        ],
    )

    result = CliRunner().invoke(app, ["disk-usage"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "TOTAL" in result.stdout
    # 1024 + 3072 = 4096 bytes → "4.0 KB" via humanize()
    assert "4.0 KB" in result.stdout


def test_disk_usage_na_row_renders_and_footer_skips_it(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.maintenance import DiskRow

    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.maintenance.gather_disk_usage",
        return_value=[
            DiskRow(component="Golden images", size_bytes=1024, path="incus image list"),
            DiskRow(component="Containers", size_bytes=None, path="/pool/containers"),
        ],
    )

    result = CliRunner().invoke(app, ["disk-usage"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "n/a" in result.stdout  # None row is not a misleading 0.0 B
    # footer totals only the measurable row (1024 B), unaffected by the n/a
    assert "1.0 KB" in result.stdout


def test_disk_usage_na_row_json_is_null(tmp_path, mocker):
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.maintenance import DiskRow

    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.maintenance.gather_disk_usage",
        return_value=[DiskRow(component="Containers", size_bytes=None, path="/pool/containers")],
    )

    result = CliRunner().invoke(app, ["disk-usage", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data == [{"component": "Containers", "size_bytes": None, "path": "/pool/containers"}]


def test_disk_usage_format_json(tmp_path, mocker):
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.maintenance import DiskRow

    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"shared_dir": tmp_path / "shared"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.MagicMock(),
    )
    mocker.patch(
        "jailbee.maintenance.gather_disk_usage",
        return_value=[
            DiskRow(component="Images", size_bytes=2048, path="/var/lib/incus/images"),
            DiskRow(component="Containers", size_bytes=4096, path="/var/lib/incus/containers"),
        ],
    )

    result = CliRunner().invoke(app, ["disk-usage", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data == [
        {"component": "Images", "size_bytes": 2048, "path": "/var/lib/incus/images"},
        {"component": "Containers", "size_bytes": 4096, "path": "/var/lib/incus/containers"},
    ]


def test_snapshot_ls_format_json(tmp_path, mocker):
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base"],
            "state": None,
            "config": {},
        }
    ]
    incus_mock.return_value.snapshot_list.return_value = [
        {"name": "snap-1", "created_at": "2026-05-01T12:00:00Z"},
    ]

    result = CliRunner().invoke(app, ["snapshot", "ls", "feat-x", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data == [{"name": "snap-1", "created": "2026-05-01T12:00:00Z"}]


def test_snapshot_ls_format_json_empty_returns_empty_list(tmp_path, mocker):
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base"],
            "state": None,
            "config": {},
        }
    ]
    incus_mock.return_value.snapshot_list.return_value = []

    result = CliRunner().invoke(app, ["snapshot", "ls", "feat-x", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert _json.loads(result.stdout) == []


def test_pool_prune_sums_across_multiple_pools(tmp_path, mocker):
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch(
        "jailbee.pool.pools_for",
        return_value=[_fake_pool("gradle"), _fake_pool("chrome-profile")],
    )
    prune = mocker.patch("jailbee.pool.prune", side_effect=[2, 3])
    result = runner.invoke(app, ["pool", "prune"])
    assert result.exit_code == 0, result.stdout
    assert "Pruned 5 free slots" in result.stdout
    assert prune.call_count == 2


def test_pool_prune_rejects_an_unknown_pool_name(tmp_path, mocker):
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch("jailbee.pool.pools_for", return_value=[])
    result = runner.invoke(app, ["pool", "prune", "nosuch"])
    assert result.exit_code == 2
    assert "nosuch" in result.output


def test_chrome_pool_alias_still_works(tmp_path, mocker):
    """The old `chrome-pool prune` name keeps working, as a deprecated alias."""
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    prune = mocker.patch("jailbee.pool.prune", return_value=0)
    mocker.patch("jailbee.pool.get", return_value=_fake_pool("chrome-profile"))
    result = runner.invoke(app, ["chrome-pool", "prune"])
    assert result.exit_code == 0
    assert "deprecated" in result.stdout.lower()
    prune.assert_called_once()


def test_chrome_pool_ls_alias_still_works(tmp_path, mocker):
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_config(tmp_path))
    mocker.patch("jailbee.pool.get", return_value=_fake_pool("chrome-profile"))
    mocker.patch("jailbee.pool.list_slots", return_value=[])
    mocker.patch("jailbee.pool.unique_bytes", return_value=0)
    result = runner.invoke(app, ["chrome-pool", "ls"])
    assert result.exit_code == 0, result.stdout
    assert "deprecated" in result.stdout.lower()


def test_chrome_pool_ls_alias_keeps_its_help_text():
    """The alias's --format/--fields options must keep the help strings and
    --format completion the pre-generalisation `chrome-pool ls` had — a fix
    round found them dropped from the delegating alias's own Option()s."""
    result = runner.invoke(app, ["chrome-pool", "ls", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "Output format: table (default) or json." in result.stdout
    assert "Allowed: pool, slot, container, warmth_mtime" in result.stdout


def test_ls_without_dot_jailbee_config_synthesizes_by_default(tmp_path, monkeypatch, mocker):
    """Superseded expectation: this used to assert `ls` exits 1 in a
    config-less directory. Scratch-config synthesis (`scratch.enabled`
    defaults to true in `global.yaml`) makes that exactly the case this
    feature is meant to fix — see `test_ls_works_in_a_directory_with_no_config`
    below for the discriminating version of that scenario. This test now
    covers the distinct case that one: no `.git` directory at all. Scratch
    synthesis has no git dependency (`detect_upstream_remote`/
    `detect_default_branch` degrade to a fallback when there is no repo), so
    `ls` must still succeed here."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _scratch_cwd(tmp_path, monkeypatch, mocker, git=False)
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = []

    result = CliRunner().invoke(app, ["ls"])

    assert result.exit_code == 0, result.output


# ---- multi-repo CLI behavior ----


def _setup_repo(tmp_path, name="myrepo"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("{}\n")
    return repo


def test_ls_default_filters_to_own_repo(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {},
        },
        {
            "name": "other-feat-y",
            "status": "Running",
            "profiles": ["default", "other-base", "other-binds", "other-net-strict"],
            "state": None,
            "config": {},
        },
    ]
    incus_mock.return_value.config_get.return_value = None
    runner = CliRunner()
    result = runner.invoke(app, ["ls"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    # NAME column shows the short name (prefix stripped); the prefix never appears.
    assert "feat-x" in result.stdout
    assert "myrepo-feat-x" not in result.stdout
    assert "myrepo" not in result.stdout
    assert "feat-y" not in result.stdout


def _setup_repo_with_columns(tmp_path, yaml_body: str, name="myrepo"):
    """A repo whose .gie/config.yaml carries an `ls:` / `dashboard:` block."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text(yaml_body)
    return repo


def _one_container(mocker):
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {},
        }
    ]
    incus_mock.return_value.config_get.return_value = None
    return incus_mock


def test_ls_honours_the_configured_field_list(mocker, tmp_path):
    """`ls.fields` picks the columns when --fields is absent."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, state]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "STATE" in result.stdout
    assert "NETWORK" not in result.stdout


def test_ls_fields_flag_beats_the_configured_list(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, state]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls", "--fields", "name,network"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "NETWORK" in result.stdout
    assert "STATE" not in result.stdout


def test_ls_hide_drops_a_default_column(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  hide: [network]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "NETWORK" not in result.stdout
    assert "STATE" in result.stdout


def test_ls_can_configure_an_off_by_default_column(mocker, tmp_path):
    """`local_diff` is default_table=False — naming it in config brings it in."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, local_diff]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "LOCAL" in result.stdout


def test_ls_configured_field_list_overrides_show_if(mocker, tmp_path):
    """Naming a dynamic column in `ls.fields` renders it even when its
    `show_if` is false — the container in `_one_container` has no PR, but
    `pr` was named explicitly, so it must still appear."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, pr]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "PR" in result.stdout


def test_ls_fields_flag_reaches_a_column_the_config_hides(mocker, tmp_path):
    """A hidden column only loses default_table; --fields can still name it."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  hide: [network]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls", "--fields", "name,network"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "NETWORK" in result.stdout


def _write_global_columns(tmp_path, monkeypatch, yaml_body: str):
    """Point $XDG_CONFIG_HOME at a tmp global.yaml carrying `yaml_body`."""
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text(yaml_body)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))


def test_ls_honours_a_global_only_field_list(mocker, tmp_path, monkeypatch):
    """`global.yaml` is the documented normal home for the setting, so a
    global-only block must reach `gie ls`. Both files are written here — the
    older tests only ever wrote the repo file, which is how a merge bug in
    the global layer stayed invisible."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _write_global_columns(tmp_path, monkeypatch, "ls:\n  fields: [name, state]\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "STATE" in result.stdout
    assert "NETWORK" not in result.stdout


def test_ls_repo_field_list_replaces_the_global_one(mocker, tmp_path, monkeypatch):
    """Not appends: routing these blocks through `deep_merge`'s list rule
    produced `[name, state, name, network]` and rendered NAME twice."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _write_global_columns(tmp_path, monkeypatch, "ls:\n  fields: [name, state]\n")
    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, network]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    assert "NETWORK" in result.stdout
    assert "STATE" not in result.stdout
    assert result.stdout.count("NAME") == 1


def test_ls_reports_a_broken_global_config_instead_of_a_traceback(mocker, tmp_path, monkeypatch):
    """`ls` used to call `load_global_config` directly. A host-level error
    passes `load_config` cleanly (it only merges the Config-level subset) and
    then raised out of Typer's standalone mode as a traceback."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _write_global_columns(tmp_path, monkeypatch, "docker_registry_mirror:\n  port: not-a-number\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "docker_registry_mirror" in combined or "port" in combined
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_ls_warns_but_still_runs_on_a_global_column_typo(mocker, tmp_path, monkeypatch):
    """An unknown column name in `global.yaml` is a personal display
    preference, not a reason to break `gie ls` for everyone: it warns and
    proceeds with the remaining valid names, rather than exiting 1 like a
    genuine host-level error (see the test above)."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _write_global_columns(tmp_path, monkeypatch, "ls:\n  fields: [name, nosuchfield]\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "nosuchfield" in combined
    assert "NAME" in result.stdout


def test_ls_global_column_typo_warns_only_once(mocker, tmp_path, monkeypatch):
    """`_load_global()` is called exactly once by `gie ls`; the warning must
    not be duplicated even if that changes — assert the count, not just
    presence, so a future double-call regresses visibly."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _write_global_columns(tmp_path, monkeypatch, "ls:\n  fields: [name, nosuchfield]\n")
    repo = _setup_repo_with_columns(tmp_path, "{}\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert combined.count("nosuchfield") == 1


def test_ls_repo_column_typo_warns_only_once(mocker, tmp_path):
    """The repo-layer mirror of `test_ls_global_column_typo_warns_only_once`:
    `_load_or_exit()` is called exactly once by `gie ls`, so a repo-layer
    column typo's warning must not be duplicated either."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, nosuchfield]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert combined.count("nosuchfield") == 1


def test_ls_json_ignores_the_configured_field_list(mocker, tmp_path):
    """`ls.fields` is a table display preference; JSON keeps its default_json set."""
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_columns(tmp_path, "ls:\n  fields: [name, state]\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    _one_container(mocker)

    result = CliRunner().invoke(app, ["ls", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert len(data) == 1
    # The built-in default_json set survives — not narrowed to name/state.
    assert "network" in data[0]
    assert "memory_limit" in data[0]


def test_ls_format_json_emits_machine_readable_output(mocker, tmp_path):
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Stopped",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {"limits.memory": "8GB"},
        },
    ]
    incus_mock.return_value.config_get.return_value = None

    result = CliRunner().invoke(app, ["ls", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert len(data) == 1
    row = data[0]
    assert row["name"] == "feat-x"
    assert row["state"] == "Stopped"
    assert row["mode"] == "clone"
    assert row["network"] == "strict"
    assert row["memory_limit"] == "8GB"
    # By default JSON omits the flat git fields; git_status is not in default JSON
    # either because it's only useful with --fields. WT etc. are table-only.
    assert "wt" not in row
    assert "ahead_diff" not in row


def test_ls_fields_filters_columns_and_supports_git_status_nested(mocker, tmp_path):
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import GitStatus

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            # `user.jailbee.repo_dir` is what makes this container a probe target
            # (`lifecycle.list_containers` skips containers without one), so
            # the stubbed `probe_many_parallel` result below is actually used.
            "config": {"user.jailbee.repo_dir": "/home/dev/myrepo"},
        },
    ]
    incus_mock.return_value.config_get.return_value = None
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={
            "myrepo-feat-x": GitStatus(
                wt="clean", ahead_diff="+1 -0", ahead_count="1", conflict="ok"
            ),
        },
    )

    result = CliRunner().invoke(
        app,
        ["ls", "--format", "json", "--fields", "name,git_status"],
    )
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data == [
        {
            "name": "feat-x",
            "git_status": {
                "wt": "clean",
                "ahead_diff": "+1 -0",
                "ahead_count": "1",
                "conflict": "ok",
                "head_sha": "",
                "remote_contained": None,
                "local_diff": "?",
                "local_count": "?",
            },
        }
    ]


def test_ls_fields_unknown_returns_error(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = []
    incus_mock.return_value.config_get.return_value = None

    result = CliRunner().invoke(app, ["ls", "--fields", "name,bogus"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "bogus" in combined


def test_ls_fields_help_lists_every_known_field_name() -> None:
    """The --fields help text (cli.py) is a hand-maintained list; it must
    stay in sync with the real field names in lifecycle.ls_field_specs, or
    a real, usable field (e.g. conflict, local_diff, local_count) silently
    drops out of the user-facing documentation."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ls_field_specs

    result = CliRunner().invoke(app, ["ls", "--help"])
    assert result.exit_code == 0

    known = {f.name for f in ls_field_specs(now=datetime.now(UTC), all_repos=True)}
    for name in known:
        assert name in result.stdout, f"{name!r} missing from --fields help text"


# ---- interactive container picker ----


def _gie_container_payload(name: str) -> dict:
    return {
        "name": name,
        "status": "Running",
        "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        "state": {"network": {"eth0": {"addresses": [{"address": "10.0.0.5", "family": "inet"}]}}},
        "config": {"limits.memory": "4GB"},
    }


def test_tmux_command_attaches_to_session(mocker):
    """`gie tmux <container>` calls incus exec_interactive with tmux attach."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = mocker.MagicMock()
    # No autostart agents, so select_window isn't called.
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exec_interactive.return_value = 0
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(incus, "test-feat"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tmux", "test-feat"])

    assert result.exit_code == 0, result.stdout
    incus.exec_interactive.assert_called_once()
    container_arg, cmd_arg = incus.exec_interactive.call_args.args
    assert container_arg == "test-feat"
    cmd_str = " ".join(cmd_arg)
    assert "tmux attach" in cmd_str
    assert "autostart" in cmd_str
    # Routed through `incus exec --user`, not `sudo -i`, so user-defined
    # container.env entries on the base profile survive into the tmux
    # session. HOME must be passed explicitly (incus exec --user UID does
    # not derive it from /etc/passwd).
    kwargs = incus.exec_interactive.call_args.kwargs
    assert kwargs["env"] == {"HOME": "/home/dev", "USER": "dev", "LOGNAME": "dev"}
    assert kwargs["uid"] is not None
    assert "sudo" not in cmd_arg


def test_tmux_command_creates_session_if_missing(mocker):
    """`gie tmux <container>` creates the autostart session if it doesn't exist.

    Without this, containers without autostart steps (or where autostart
    hasn't run yet) would fail to attach because the session is missing.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.incus import IncusError

    cfg = mocker.MagicMock()
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exec_interactive.return_value = 0

    # First exec call (has-session check) raises; subsequent creation calls succeed.
    incus.exec.side_effect = [IncusError("no session"), "", "", ""]
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(incus, "test-feat"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tmux", "test-feat"])

    assert result.exit_code == 0, result.stdout
    exec_calls = [" ".join(c.args[1]) for c in incus.exec.call_args_list]
    assert any("has-session" in c for c in exec_calls)
    assert any("new-session" in c for c in exec_calls)
    incus.exec_interactive.assert_called_once()


def test_tmux_command_propagates_exit_code(mocker):
    """If tmux attach fails (no session), exit code surfaces."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = mocker.MagicMock()
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exec_interactive.return_value = 1
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(incus, "test-feat"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tmux", "test-feat"])
    assert result.exit_code == 1


def test_tmux_command_focuses_claude_window_when_autostart(mocker):
    """When an agent has autostart on, `gie tmux` selects that agent's
    window (the last one when several autostart) before attaching so
    users land in it directly."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.config import AutostartStep

    cfg = mocker.MagicMock()
    mocker.patch(
        "jailbee.autostart.agent_autostart_steps",
        return_value=[AutostartStep(name="claude", run="exec claude", background=True)],
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exec_interactive.return_value = 0
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(incus, "test-feat"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tmux", "test-feat"])

    assert result.exit_code == 0, result.stdout
    exec_calls = [" ".join(c.args[1]) for c in incus.exec.call_args_list]
    assert any("select-window" in c and "autostart:claude" in c for c in exec_calls)


def test_tmux_waits_for_in_flight_container(mocker):
    """`gie tmux <name>` routes through _resolve_attachable (waits, then attaches)."""
    from typer.testing import CliRunner

    from jailbee import cli

    incus = mocker.MagicMock()
    resolve = mocker.patch.object(
        cli, "_resolve_attachable", return_value=(incus, "myrepo-feat-bg")
    )
    mocker.patch.object(cli, "_load_or_exit", return_value=mocker.MagicMock())
    attach = mocker.patch.object(cli, "_attach_tmux", return_value=0)

    result = CliRunner().invoke(cli.app, ["tmux", "feat-bg"])

    assert result.exit_code == 0
    resolve.assert_called_once()
    attach.assert_called_once()


def test_tmux_force_flag_passes_through(mocker):
    """`gie tmux <name> --force` forwards force=True to _resolve_attachable."""
    from jailbee import cli

    incus = mocker.MagicMock()
    resolve = mocker.patch.object(
        cli, "_resolve_attachable", return_value=(incus, "myrepo-feat-bg")
    )
    mocker.patch.object(cli, "_load_or_exit", return_value=mocker.MagicMock())
    mocker.patch.object(cli, "_attach_tmux", return_value=0)

    result = CliRunner().invoke(cli.app, ["tmux", "feat-bg", "--force"])

    assert result.exit_code == 0
    assert resolve.call_args.kwargs.get("force") is True


def test_shell_force_flag_passes_through(mocker):
    """`gie shell <name> --force` forwards force=True to _resolve_attachable."""
    from jailbee import cli

    incus = mocker.MagicMock()
    resolve = mocker.patch.object(
        cli, "_resolve_attachable", return_value=(incus, "myrepo-feat-bg")
    )
    mocker.patch.object(cli, "_load_or_exit", return_value=mocker.MagicMock())
    mocker.patch.object(cli, "_attach_shell", return_value=0)

    result = CliRunner().invoke(cli.app, ["shell", "feat-bg", "--force"])

    assert result.exit_code == 0
    assert resolve.call_args.kwargs.get("force") is True


def test_tmux_without_force_defaults_false(mocker):
    """No --force -> _resolve_attachable called with force=False."""
    from jailbee import cli

    incus = mocker.MagicMock()
    resolve = mocker.patch.object(
        cli, "_resolve_attachable", return_value=(incus, "myrepo-feat-bg")
    )
    mocker.patch.object(cli, "_load_or_exit", return_value=mocker.MagicMock())
    mocker.patch.object(cli, "_attach_tmux", return_value=0)

    result = CliRunner().invoke(cli.app, ["tmux", "feat-bg"])

    assert result.exit_code == 0
    assert resolve.call_args.kwargs.get("force") is False


def _mock_attach_guard(mocker, *, exists=True, wait_error=None, row=None, tty=True):
    """Mock what `_resolve_attachable` reaches for; return its `Incus` instance.

    ``wait_error`` is what `wait_for_background_ready` raises — a ValueError
    for a failed/stale job, a KeyboardInterrupt for Ctrl-C, None for a wait
    that succeeds. ``tty`` drives the stdin check the confirmation is gated on
    (pytest's captured stdin is not a TTY, so tests that want the prompt must
    say so).
    """
    incus = mocker.MagicMock()
    incus.exists.return_value = exists
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive",
        return_value="myrepo-feat-bg",
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-bg")
    mocker.patch("jailbee.lifecycle.lookup_background_job", return_value=row)
    mocker.patch("jailbee.tui.console")
    mocker.patch("jailbee.lifecycle.wait_for_background_ready", side_effect=wait_error)
    mocker.patch("jailbee.cli.sys.stdin.isatty", return_value=tty)
    return incus


def _failed_job_row(mocker, *, phase="failed", pid=999, error_msg="boom"):
    return mocker.MagicMock(phase=phase, pid=pid, error_msg=error_msg)


def test_resolve_attachable_ready_job_never_asks(mocker):
    """The healthy path is untouched: no warning, no prompt, just the name."""
    from jailbee import cli

    _mock_attach_guard(mocker)
    warn = mocker.patch("jailbee.cli.warn")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    _incus, name = cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert name == "myrepo-feat-bg"
    warn.assert_not_called()
    confirm.assert_not_called()


def test_resolve_attachable_failed_job_attaches_after_confirmation(mocker):
    """A failed job over a running container is a warning + a prompt, not a
    refusal: the container is exactly where the user needs to look."""
    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("background creation of 'feat-bg' failed: boom"),
        row=_failed_job_row(mocker),
    )
    warn = mocker.patch("jailbee.cli.warn")
    info = mocker.patch("jailbee.cli.info")
    confirm = mocker.patch("jailbee.cli.typer.confirm", return_value=True)

    _incus, name = cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert name == "myrepo-feat-bg"
    assert "failed: boom" in warn.call_args[0][0]
    confirm.assert_called_once()
    hints = " ".join(str(c.args[0]) for c in info.call_args_list)
    assert "jailbee job clear feat-bg" in hints


def test_resolve_attachable_failed_job_declined_exits(mocker):
    """Answering no to the prompt is still a clean Exit 1."""
    import typer

    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("background creation of 'feat-bg' failed: boom"),
        row=_failed_job_row(mocker),
    )
    mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.info")
    confirm = mocker.patch("jailbee.cli.typer.confirm", return_value=False)

    with pytest.raises(typer.Exit) as exc_info:
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg")
    assert exc_info.value.exit_code == 1
    confirm.assert_called_once()


def test_resolve_attachable_force_skips_the_confirmation(mocker):
    """`--force` no longer unlocks the attach — it only skips the question."""
    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("background creation of 'feat-bg' failed: boom"),
        row=_failed_job_row(mocker),
    )
    mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.info")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    _incus, name = cli._resolve_attachable(mocker.MagicMock(), "feat-bg", force=True)

    assert name == "myrepo-feat-bg"
    confirm.assert_not_called()


def test_resolve_attachable_without_a_tty_skips_the_confirmation(mocker):
    """No TTY (a script, a detached dashboard child) means `typer.confirm`
    would read EOF and abort the attach the caller explicitly asked for."""
    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("background creation of 'feat-bg' failed: boom"),
        row=_failed_job_row(mocker),
        tty=False,
    )
    mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.info")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    _incus, name = cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert name == "myrepo-feat-bg"
    confirm.assert_not_called()


def test_resolve_attachable_never_hints_at_force(mocker):
    """The old escape hatch is gone; nothing should still advertise it."""
    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("background creation of 'feat-bg' failed: boom"),
        row=_failed_job_row(mocker),
    )
    warn = mocker.patch("jailbee.cli.warn")
    info = mocker.patch("jailbee.cli.info")
    mocker.patch("jailbee.cli.typer.confirm", return_value=True)

    cli._resolve_attachable(mocker.MagicMock(), "feat-bg", attach_cmd="tmux")

    printed = " ".join(str(c.args[0]) for c in [*warn.call_args_list, *info.call_args_list])
    assert "--force" not in printed


def test_resolve_attachable_dead_worker_attaches_after_confirmation(mocker):
    """A non-terminal phase whose worker vanished is recoverable the same way."""
    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("background worker for 'feat-bg' is gone (last phase: cloning)"),
        row=_failed_job_row(mocker, phase="cloning", error_msg=None),
    )
    mocker.patch("jailbee.background.worker_alive", return_value=False)
    mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.info")
    confirm = mocker.patch("jailbee.cli.typer.confirm", return_value=True)

    _incus, name = cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert name == "myrepo-feat-bg"
    confirm.assert_called_once()


def test_resolve_attachable_live_destroy_job_is_not_offered(mocker):
    """A destroy whose worker is still running is not a broken job to inspect —
    attaching to a container mid-teardown helps nobody."""
    import typer

    from jailbee import cli

    _mock_attach_guard(
        mocker,
        wait_error=ValueError("'feat-bg' is being destroyed"),
        row=_failed_job_row(mocker, phase="starting", error_msg=None),
    )
    mocker.patch("jailbee.background.worker_alive", return_value=True)
    err = mocker.patch("jailbee.cli.error")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    with pytest.raises(typer.Exit) as exc_info:
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert exc_info.value.exit_code == 1
    confirm.assert_not_called()
    assert "being destroyed" in err.call_args[0][0]


def test_resolve_attachable_failed_job_without_a_container_exits(mocker):
    """A create that died before `incus init` has nothing to attach to; say so
    and point at the leftover job record instead of prompting."""
    import typer

    from jailbee import cli

    _mock_attach_guard(
        mocker,
        exists=False,
        wait_error=ValueError("background creation of 'feat-bg' failed: declined"),
        row=_failed_job_row(mocker, error_msg="declined"),
    )
    err = mocker.patch("jailbee.cli.error")
    info = mocker.patch("jailbee.cli.info")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    with pytest.raises(typer.Exit) as exc_info:
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert exc_info.value.exit_code == 1
    confirm.assert_not_called()
    assert "failed: declined" in err.call_args[0][0]
    hints = " ".join(str(c.args[0]) for c in info.call_args_list)
    assert "jailbee job clear feat-bg" in hints


def test_resolve_attachable_keyboard_interrupt_offers_the_running_container(mocker):
    """Ctrl-C out of the wait, container already up: offer to look inside it
    anyway — this is the escape hatch `--force` used to be."""
    from jailbee import cli

    _mock_attach_guard(mocker, wait_error=KeyboardInterrupt)
    warn = mocker.patch("jailbee.cli.warn")
    confirm = mocker.patch("jailbee.cli.typer.confirm", return_value=True)

    _incus, name = cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert name == "myrepo-feat-bg"
    warn.assert_called_once()
    confirm.assert_called_once()


def test_resolve_attachable_keyboard_interrupt_names_a_create(mocker):
    """The wording tells the user what they walked away from."""
    from jailbee import cli
    from jailbee.db.models import JOB_CREATE

    _mock_attach_guard(
        mocker,
        wait_error=KeyboardInterrupt,
        row=mocker.MagicMock(phase="autostart", pid=1, op_kind=JOB_CREATE),
    )
    warn = mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.typer.confirm", return_value=True)

    cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert "still being created" in warn.call_args[0][0]


def test_resolve_attachable_keyboard_interrupt_names_a_boot(mocker):
    """A background `jailbee restart` is not a creation — an interrupted wait
    over one must not tell the user their container is being built."""
    from jailbee import cli
    from jailbee.db.models import JOB_BOOT

    _mock_attach_guard(
        mocker,
        wait_error=KeyboardInterrupt,
        row=mocker.MagicMock(phase="autostart", pid=1, op_kind=JOB_BOOT),
    )
    warn = mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.typer.confirm", return_value=True)

    cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert "still booting" in warn.call_args[0][0]


def test_resolve_attachable_keyboard_interrupt_declined_exits(mocker):
    import typer

    from jailbee import cli

    _mock_attach_guard(mocker, wait_error=KeyboardInterrupt)
    mocker.patch("jailbee.cli.warn")
    confirm = mocker.patch("jailbee.cli.typer.confirm", return_value=False)

    with pytest.raises(typer.Exit) as exc_info:
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg")
    assert exc_info.value.exit_code == 1
    confirm.assert_called_once()


def test_resolve_attachable_keyboard_interrupt_still_asks_under_force(mocker):
    """Ctrl-C is an explicit cancel, so `--force` must not answer it. Without
    this the dashboard (which always passes `--force`) would turn a cancelled
    wait into an attach to a half-built container."""
    import typer

    from jailbee import cli

    _mock_attach_guard(mocker, wait_error=KeyboardInterrupt)
    mocker.patch("jailbee.cli.warn")
    confirm = mocker.patch("jailbee.cli.typer.confirm", return_value=False)

    with pytest.raises(typer.Exit):
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg", force=True)
    confirm.assert_called_once()


def test_resolve_attachable_keyboard_interrupt_without_a_tty_exits(mocker):
    """Nobody is there to say yes, and Ctrl-C already said no."""
    import typer

    from jailbee import cli

    _mock_attach_guard(mocker, wait_error=KeyboardInterrupt, tty=False)
    mocker.patch("jailbee.cli.warn")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    with pytest.raises(typer.Exit) as exc_info:
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert exc_info.value.exit_code == 1
    confirm.assert_not_called()


def test_resolve_attachable_keyboard_interrupt_without_a_container_exits(mocker):
    """Nothing exists yet, so there is nothing to offer — warn and exit."""
    import typer

    from jailbee import cli

    _mock_attach_guard(mocker, exists=False, wait_error=KeyboardInterrupt)
    warn = mocker.patch("jailbee.cli.warn")
    confirm = mocker.patch("jailbee.cli.typer.confirm")

    with pytest.raises(typer.Exit) as exc_info:
        cli._resolve_attachable(mocker.MagicMock(), "feat-bg")

    assert exc_info.value.exit_code == 1
    confirm.assert_not_called()
    assert "jailbee ls" in warn.call_args[0][0]


def test_shell_without_name_auto_picks_single_container(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        _gie_container_payload("myrepo-feat-only"),
    ]
    incus_mock.return_value.exec_interactive.return_value = 0

    runner = CliRunner()
    result = runner.invoke(app, ["shell"])

    assert result.exit_code == 0, result.stdout
    import os

    incus_mock.return_value.exec_interactive.assert_called_once_with(
        "myrepo-feat-only",
        ["bash", "-c", "cd /home/dev/myrepo 2>/dev/null; exec bash -l"],
        uid=os.getuid(),
        gid=os.getgid(),
        env={"HOME": "/home/dev", "USER": "dev", "LOGNAME": "dev"},
        init_groups=True,
    )


def test_start_without_name_auto_picks(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        _gie_container_payload("myrepo-feat-only"),
    ]
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=False)

    runner = CliRunner()
    result = runner.invoke(app, ["start", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    incus_mock.return_value.start.assert_called_once_with("myrepo-feat-only")


def test_stop_bounds_the_clean_shutdown(mocker, tmp_path):
    """`jb stop` must not sit silently on incusd's 600s shutdown budget."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.stopping import CLEAN_STOP_BUDGET

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.exists.side_effect = lambda n: n == "myrepo-feat-bar"

    result = CliRunner().invoke(app, ["stop", "feat-bar"])

    assert result.exit_code == 0, result.stdout
    incus_mock.return_value.stop.assert_called_once_with(
        "myrepo-feat-bar", timeout=CLEAN_STOP_BUDGET
    )


def test_stop_reports_a_container_that_will_not_shut_down(mocker, tmp_path):
    """No silent force: the user's container may hold unsaved work."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.incus import IncusError

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.exists.side_effect = lambda n: n == "myrepo-feat-bar"
    incus_mock.return_value.exec.return_value = ""
    incus_mock.return_value.console_log.return_value = ""
    incus_mock.return_value.stop.side_effect = IncusError(
        "`incus stop myrepo-feat-bar` failed (exit 1): Error: Failed shutting down "
        'instance, status is "Running": context deadline exceeded'
    )

    result = CliRunner().invoke(app, ["stop", "feat-bar"])

    assert result.exit_code != 0
    # `entry.main` (bypassed by CliRunner) is what prints an IncusError for
    # the user; what matters here is that the message it will print names a
    # way forward instead of just echoing incus's deadline.
    assert isinstance(result.exception, IncusError)
    message = str(result.exception)
    assert "--force" in message
    assert "incus console --show-log myrepo-feat-bar" in message
    assert mocker.call("myrepo-feat-bar", force=True) not in (
        incus_mock.return_value.stop.call_args_list
    )


def test_shell_with_explicit_name_still_works(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.exists.side_effect = lambda n: n == "myrepo-feat-bar"
    incus_mock.return_value.exec_interactive.return_value = 0

    runner = CliRunner()
    result = runner.invoke(app, ["shell", "feat-bar"])

    assert result.exit_code == 0, result.stdout
    import os

    incus_mock.return_value.exec_interactive.assert_called_once_with(
        "myrepo-feat-bar",
        ["bash", "-c", "cd /home/dev/myrepo 2>/dev/null; exec bash -l"],
        uid=os.getuid(),
        gid=os.getgid(),
        env={"HOME": "/home/dev", "USER": "dev", "LOGNAME": "dev"},
        init_groups=True,
    )


def test_ls_all_shows_every_repo(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {},
        },
        {
            "name": "other-feat-y",
            "status": "Running",
            "profiles": ["default", "other-base", "other-binds", "other-net-strict"],
            "state": None,
            "config": {},
        },
    ]
    incus_mock.return_value.config_get.return_value = None
    runner = CliRunner()
    result = runner.invoke(app, ["ls", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    # NAME column carries short names; REPO column carries the prefix.
    assert "feat-x" in result.stdout
    assert "feat-y" in result.stdout
    assert "myrepo-feat-x" not in result.stdout
    assert "other-feat-y" not in result.stdout
    assert "myrepo" in result.stdout
    assert "other" in result.stdout


def test_shell_resolves_short_name(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    runner = CliRunner()
    result = runner.invoke(app, ["shell", "feat-x"])
    assert result.exit_code == 0, result.stdout
    incus.exec_interactive.assert_called_once()
    args = incus.exec_interactive.call_args.args
    assert args[0] == "myrepo-feat-x"


def test_shell_root_user_lands_in_repo_dir(mocker, tmp_path):
    """`gie shell -u root` runs as uid 0 via `incus exec --user 0`."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    result = CliRunner().invoke(app, ["shell", "feat-x", "--user", "root"])
    assert result.exit_code == 0, result.stdout
    incus.exec_interactive.assert_called_once_with(
        "myrepo-feat-x",
        ["bash", "-c", "cd /home/dev/myrepo 2>/dev/null; exec bash -l"],
        uid=0,
        gid=0,
        env={"HOME": "/root", "USER": "root", "LOGNAME": "root"},
        init_groups=True,
    )


# ---- gie exec ----


def test_exec_default_cwd_is_container_repo_dir(mocker, tmp_path):
    """`gie exec smoke -- claude` should run as dev under `cd <repo> && exec claude`."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    result = CliRunner().invoke(app, ["exec", "feat-x", "--", "claude"])
    assert result.exit_code == 0, result.stdout
    import os

    incus.exec_interactive.assert_called_once_with(
        "myrepo-feat-x",
        ["bash", "-lc", "cd /home/dev/myrepo && exec claude"],
        uid=os.getuid(),
        gid=os.getgid(),
        env={"HOME": "/home/dev", "USER": "dev", "LOGNAME": "dev"},
        init_groups=True,
    )


def test_exec_uses_a_login_shell_so_local_bin_is_on_path(mocker, tmp_path):
    """`jailbee exec X -- claude` found no binary under a non-login `bash -c`.

    `incus exec` supplies a bare default PATH and per-user tools live in
    ~/.local/bin, which only `/etc/profile.d/local-bin.sh` adds — so the
    command's own documented example failed with "command not found".
    `jailbee shell` and the PR-text bridge both already use a login shell.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    result = CliRunner().invoke(app, ["exec", "feat-x", "--", "claude", "--version"])
    assert result.exit_code == 0, result.stdout

    argv = incus.exec_interactive.call_args.args[1]
    assert argv[:2] == ["bash", "-lc"]


def test_exec_cwd_home(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    result = CliRunner().invoke(app, ["exec", "feat-x", "--cwd", "home", "--", "ls", "-la"])
    assert result.exit_code == 0, result.stdout
    inner = incus.exec_interactive.call_args.args[1][-1]
    assert inner == "cd /home/dev && exec ls -la"


def test_exec_cwd_explicit_path(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    result = CliRunner().invoke(app, ["exec", "feat-x", "--cwd", "/opt/foo bar", "--", "whoami"])
    assert result.exit_code == 0, result.stdout
    inner = incus.exec_interactive.call_args.args[1][-1]
    # Path is shlex-quoted because of the embedded space.
    assert inner == "cd '/opt/foo bar' && exec whoami"


def test_exec_propagates_exit_code(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 42

    result = CliRunner().invoke(app, ["exec", "feat-x", "--", "false"])
    assert result.exit_code == 42


def test_exec_quotes_command_arguments(mocker, tmp_path):
    """Args with spaces / shell metas must be shlex-quoted in the inner command."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus = incus_mock.return_value
    incus.exists.side_effect = lambda n: n == "myrepo-feat-x"
    incus.exec_interactive.return_value = 0

    result = CliRunner().invoke(app, ["exec", "feat-x", "--", "echo", "hello world", "$HOME"])
    assert result.exit_code == 0, result.stdout
    inner = incus.exec_interactive.call_args.args[1][-1]
    assert inner == "cd /home/dev/myrepo && exec echo 'hello world' '$HOME'"


def test_new_cmd_forwards_mirror_endpoint_to_lifecycle(tmp_path, mocker):
    """gie new should resolve the mirror endpoint and pass it via
    NewContainerOptions to lifecycle.new_container.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text("golden:\n  stacks:\n    docker: true\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")

    # Point gcfg at tmp_path and create the mirror CA file there so the
    # new_cmd pre-flight CA check passes.
    mirror_data_dir = tmp_path / "registry"
    (mirror_data_dir / "ca").mkdir(parents=True)
    (mirror_data_dir / "ca" / "ca.crt").write_text("fake-ca")
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=mirror_data_dir),
    )
    mocker.patch("jailbee.cli._load_global", return_value=gcfg)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.234.216.1", 3128),
    )

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    new_container.return_value = "myrepo-feat-x"

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart"],
    )
    assert result.exit_code == 0, result.stdout
    opts = new_container.call_args.args[2]
    assert opts.mirror_endpoint == ("10.234.216.1", 3128)
    assert opts.mirror_ca_path == mirror_data_dir / "ca" / "ca.crt"


def _setup_new_cmd_env(tmp_path, mocker, *, cfg_yaml: str = "{}\n"):
    """Shared fixture for `gie new` tests that need the mirror pre-flight
    satisfied. Returns (repo, mocked new_container) so the test can assert
    on what new_cmd dispatched after the container was created.
    """
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text(cfg_yaml)
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")

    mirror_data_dir = tmp_path / "registry"
    (mirror_data_dir / "ca").mkdir(parents=True)
    (mirror_data_dir / "ca" / "ca.crt").write_text("fake-ca")
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=mirror_data_dir),
    )
    mocker.patch("jailbee.cli._load_global", return_value=gcfg)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.234.216.1", 3128),
    )

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    new_container.return_value = "myrepo-feat-x"
    return repo, new_container


def test_new_cmd_default_does_not_attach(tmp_path, mocker):
    """`after_new` defaults to 'none' — `gie new` returns to host prompt."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker)
    attach_shell = mocker.patch("jailbee.cli._attach_shell")
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux")

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    attach_shell.assert_not_called()
    attach_tmux.assert_not_called()


def test_new_cmd_attach_tmux_flag_invokes_tmux(tmp_path, mocker):
    """`--attach tmux` opens the autostart tmux session after creation."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker)
    attach_shell = mocker.patch("jailbee.cli._attach_shell")
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux", return_value=0)

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart", "--attach", "tmux"],
    )

    assert result.exit_code == 0, result.stdout
    attach_tmux.assert_called_once()
    assert attach_tmux.call_args.args[2] == "myrepo-feat-x"
    attach_shell.assert_not_called()


def test_new_cmd_attach_shell_flag_invokes_shell(tmp_path, mocker):
    """`--attach shell` opens an interactive shell after creation."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker)
    attach_shell = mocker.patch("jailbee.cli._attach_shell", return_value=0)
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux")

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart", "--attach", "shell"],
    )

    assert result.exit_code == 0, result.stdout
    attach_shell.assert_called_once()
    assert attach_shell.call_args.args[2] == "myrepo-feat-x"
    attach_tmux.assert_not_called()


def test_new_cmd_after_new_config_drives_attach(tmp_path, mocker):
    """`after_new: tmux` in .gie/config.yaml attaches tmux without any flag."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker, cfg_yaml="after_new: tmux\n")
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux", return_value=0)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    attach_tmux.assert_called_once()


def test_new_cmd_no_attach_overrides_config(tmp_path, mocker):
    """`--no-attach` wins over `after_new: tmux` in the config."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker, cfg_yaml="after_new: tmux\n")
    attach_shell = mocker.patch("jailbee.cli._attach_shell")
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux")

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart", "--no-attach"],
    )

    assert result.exit_code == 0, result.stdout
    attach_shell.assert_not_called()
    attach_tmux.assert_not_called()


def test_new_cmd_attach_rejects_invalid_mode(tmp_path, mocker):
    """`--attach bash` exits with usage error before creating the container."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--attach", "bash"])

    assert result.exit_code == 2
    new_container.assert_not_called()


def test_new_cmd_rejects_attach_and_no_attach_together(tmp_path, mocker):
    """`--attach tmux --no-attach` is contradictory; reject early."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--attach", "tmux", "--no-attach"],
    )

    assert result.exit_code == 2
    new_container.assert_not_called()


def test_new_cmd_tmux_flag_invokes_tmux(tmp_path, mocker):
    """`--tmux` is shorthand for `--attach tmux`."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker)
    attach_shell = mocker.patch("jailbee.cli._attach_shell")
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux", return_value=0)

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart", "--tmux"],
    )

    assert result.exit_code == 0, result.stdout
    attach_tmux.assert_called_once()
    assert attach_tmux.call_args.args[2] == "myrepo-feat-x"
    attach_shell.assert_not_called()


def test_new_cmd_shell_flag_invokes_shell(tmp_path, mocker):
    """`--shell` is shorthand for `--attach shell`."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker)
    attach_shell = mocker.patch("jailbee.cli._attach_shell", return_value=0)
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux")

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart", "--shell"],
    )

    assert result.exit_code == 0, result.stdout
    attach_shell.assert_called_once()
    assert attach_shell.call_args.args[2] == "myrepo-feat-x"
    attach_tmux.assert_not_called()


def test_new_cmd_tmux_and_background_conflict(tmp_path, mocker):
    """`--tmux --background` states two intents; reject before creating."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--tmux", "--background"],
    )

    assert result.exit_code == 2
    assert "background" in result.output.lower()
    assert "--tmux" in result.output
    new_container.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        ["--shell"],
        ["--attach", "shell"],
        ["--no-attach"],
    ],
    ids=["shell-flag", "attach-long", "no-attach"],
)
def test_new_cmd_tmux_conflicts_with_other_attach_flags(tmp_path, mocker, extra):
    """Only one attach flag may be given."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--tmux", *extra])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()
    new_container.assert_not_called()


@pytest.mark.parametrize(
    "attach_flags",
    [["--no-attach"], ["--attach", "none"]],
    ids=["no-attach", "attach-none"],
)
def test_new_cmd_background_allows_no_attach(tmp_path, mocker, attach_flags):
    """`--background` agrees with either spelling of "no attach"; not a
    conflict."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--background", *attach_flags],
    )

    # Positive evidence that validation passed and the background path was
    # entered: that path's first act is an existence check, and the mocked
    # Incus class returns a truthy MagicMock from `exists()`, so the run
    # stops with "already exists". Reaching *that* message — rather than a
    # flag-conflict message — is the proof. Asserting only the absence of a
    # conflict message would also pass if the command died even earlier.
    assert "already exists" in result.output.lower()
    assert "mutually exclusive" not in result.output.lower()
    assert "cannot be combined" not in result.output.lower()


def test_new_cmd_attach_none_does_not_force_foreground(tmp_path, mocker):
    """`--attach none` in a background-by-default repo must stay on the
    background path.

    Unlike `--attach shell`/`--attach tmux` (and the `--tmux`/`--shell`
    shorthands), `--attach none` has nothing to attach to, so it must not
    override `new.background: true` the way an explicit attach does. A
    `wants_attach` that dropped the `attach_mode != "none"` conjunct would
    silently force foreground here, contradicting what docs/config.md
    promises.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker, cfg_yaml="new:\n  background: true\n")

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--attach", "none"],
    )

    # Positive evidence of the background path, not just absence of an
    # error: that path's first act is an existence check, and the mocked
    # Incus class returns a truthy MagicMock from `exists()`, so staying on
    # it surfaces as "already exists". `new_container.assert_not_called()`
    # is the honest signal that the foreground path did not run instead.
    assert "already exists" in result.output.lower()
    new_container.assert_not_called()


@pytest.mark.parametrize(
    "flags",
    [["--tmux"], ["--attach", "tmux"]],
    ids=["shorthand", "long-form"],
)
def test_new_cmd_explicit_attach_overrides_config_background(tmp_path, mocker, flags):
    """`new.background: true` in config yields to an explicit attach.

    Before this rule the same invocation was rejected as a conflict, forcing
    users of background-by-default repos to add `--no-background`.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker, cfg_yaml="new:\n  background: true\n")
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux", return_value=0)

    result = CliRunner().invoke(
        app,
        ["new", "feat/x", "--no-clone", "--no-autostart", *flags],
    )

    assert result.exit_code == 0, result.stdout
    # The background path `return`s before `new_container` is ever called,
    # so `new_container.assert_called_once()` is the proof the foreground
    # path ran instead — i.e. that the explicit attach overrode
    # `new.background: true`.
    new_container.assert_called_once()
    attach_tmux.assert_called_once()


def test_new_cmd_prompts_when_branch_exists_in_source(tmp_path, mocker):
    """`gie new feat/x` should ask before silently checking out an
    existing remote branch (no --base, no --yes)."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    # User confirms — container is created.
    result = CliRunner().invoke(app, ["new", "feat/x", "--no-autostart"], input="y\n")

    assert result.exit_code == 0, result.stdout
    assert "already exists in source repo" in result.stdout
    assert "Use existing branch 'feat/x'?" in result.stdout
    new_container.assert_called_once()


def test_new_cmd_aborts_when_user_declines_existing_branch(tmp_path, mocker):
    """Answering 'n' to the prompt should exit 0 without creating anything."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-autostart"], input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in result.stdout
    new_container.assert_not_called()


def test_new_cmd_yes_flag_skips_existing_branch_prompt(tmp_path, mocker):
    """`-y` / `--yes` skips the confirmation for scripted use."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    # No `input=` provided — if the prompt were reached, CliRunner would
    # see an empty stdin and abort. -y must short-circuit it.
    result = CliRunner().invoke(app, ["new", "feat/x", "--no-autostart", "-y"])

    assert result.exit_code == 0, result.stdout
    assert "already exists in source repo" in result.stdout
    assert "Use existing branch" not in result.stdout
    new_container.assert_called_once()


def test_new_cmd_existing_branch_with_base_prompts_and_names_the_base(tmp_path, mocker):
    """`gie new <existing> <base>` reuses the branch with that base branch.

    The base is not a fork point here, so the prompt says which base the
    container will be measured against instead of suggesting one.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    result = CliRunner().invoke(app, ["new", "feat/x", "main", "--no-autostart"], input="y\n")

    assert result.exit_code == 0, result.stdout
    assert "Use existing branch 'feat/x' with base 'main'?" in result.stdout
    opts = new_container.call_args.args[2]
    assert opts.container_branch == "feat/x"
    assert opts.base == "main"


def test_new_cmd_existing_branch_with_base_abort_creates_nothing(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    result = CliRunner().invoke(app, ["new", "feat/x", "main", "--no-autostart"], input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in result.stdout
    new_container.assert_not_called()


def test_new_cmd_existing_branch_with_base_yes_skips_prompt(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    result = CliRunner().invoke(app, ["new", "feat/x", "main", "--no-autostart", "-y"])

    assert result.exit_code == 0, result.stdout
    assert "Use existing branch" not in result.stdout
    assert new_container.call_args.args[2].base == "main"


def test_new_cmd_no_prompt_when_branch_missing_in_source(tmp_path, mocker):
    """When the branch doesn't exist on the host, no prompt — `gie new`
    creates a new branch off the default."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=False)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    assert "already exists" not in result.stdout
    new_container.assert_called_once()


def test_new_cmd_no_prompt_when_base_given_and_branch_is_new(tmp_path, mocker):
    """`gie new feat/x feat/y` with feat/x absent is an unambiguous fork —
    nothing to confirm."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch(
        "jailbee.git.branch_exists_in_source",
        side_effect=lambda _root, _remote, name: name == "feat/y",
    )

    result = CliRunner().invoke(app, ["new", "feat/x", "feat/y", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    assert "already exists" not in result.stdout
    new_container.assert_called_once()


def test_new_cmd_no_prompt_with_no_clone(tmp_path, mocker):
    """`--no-clone` skips the prompt entirely — without a clone the
    branch question is moot."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _, new_container = _setup_new_cmd_env(tmp_path, mocker)
    exists = mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    assert "already exists" not in result.stdout
    exists.assert_not_called()
    new_container.assert_called_once()


def test_init_resolves_mirror_endpoint_and_calls_run_init(tmp_path, mocker):
    """gie init should resolve the mirror endpoint and forward it to
    run_init.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text("golden:\n  stacks:\n    docker: true\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.234.216.1", 3128),
    )

    run_init = mocker.patch("jailbee.init_command.run_init")
    # `jailbee init` calls install_systemd_units() after run_init(). Left
    # unmocked it writes real unit files into ~/.config/systemd/user/ and
    # runs `systemctl --user daemon-reload` + `enable --now` against the
    # developer's own session — the units end up rendered for a pytest
    # tmp_path. Mock it here; its behaviour is covered by
    # tests/test_systemd_install.py against a redirected HOME.
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("jailbee.egress_pool.register_repo")

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout
    run_init.assert_called_once()
    assert run_init.call_args.kwargs["mirror_endpoint"] == ("10.234.216.1", 3128)


# --- `gie ide` IDE resolution (cfg.ide default + --app override) ---


def _setup_repo_with_ide(tmp_path, ide_value: str | None):
    """Build a repo whose .gie/config.yaml sets jetbrains.ide: <value>.

    Forces ``jetbrains.enabled: true`` so the ide CLI command isn't
    short-circuited by the master-switch default (`false`).
    """
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    if ide_value is None:
        body = "jetbrains:\n  enabled: true\n"
    else:
        body = f"jetbrains:\n  enabled: true\n  ide: {ide_value}\n"
    (repo / ".jailbee" / "config.yaml").write_text(body)
    return repo


def test_ide_cmd_uses_cfg_jetbrains_ide_when_no_app_flag(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_ide(tmp_path, "pycharm")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = CliRunner().invoke(app, ["ide", "feat-x"])
    assert result.exit_code == 0, result.stdout
    assert open_ide.call_args.args[3] == "pycharm"


def test_ide_cmd_errors_when_jetbrains_disabled(tmp_path, mocker):
    """`gie ide` errors with exit 2 when jetbrains.enabled is false."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("jetbrains:\n  enabled: false\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = CliRunner().invoke(app, ["ide", "feat-x"])

    assert result.exit_code == 2
    open_ide.assert_not_called()


def test_ide_cmd_defaults_to_idea_when_no_app_and_no_override(tmp_path, mocker):
    """jetbrains.ide defaults to `idea`; `gie ide` falls through to it."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_ide(tmp_path, None)
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = CliRunner().invoke(app, ["ide", "feat-x"])
    assert result.exit_code == 0, result.stdout
    assert open_ide.call_args.args[3] == "idea"


def test_ide_cmd_app_flag_overrides_cfg_ide(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_ide(tmp_path, "idea")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = CliRunner().invoke(app, ["ide", "feat-x", "--app", "pycharm"])
    assert result.exit_code == 0, result.stdout
    assert open_ide.call_args.args[3] == "pycharm"


# --- `gie chrome` URL resolution (cfg.chrome_url + CLI override) ---


def _setup_repo_with_chrome_url(tmp_path, chrome_url: str | None):
    """Forces ``chrome.enabled: true`` so the chrome CLI command isn't
    short-circuited by the master-switch default (`false`)."""
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    if chrome_url is None:
        body = "chrome:\n  enabled: true\n"
    else:
        body = f"chrome:\n  enabled: true\n  url: {chrome_url}\n"
    (repo / ".jailbee" / "config.yaml").write_text(body)
    return repo


def test_chrome_cmd_uses_cfg_chrome_url_when_no_url_arg(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_chrome_url(tmp_path, "https://example.com")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_chrome = mocker.patch("jailbee.gui.open_chrome")

    result = CliRunner().invoke(app, ["chrome", "feat-x"])
    assert result.exit_code == 0, result.stdout
    assert open_chrome.call_args.args[3] == "https://example.com"


def test_chrome_cmd_url_arg_overrides_cfg_chrome_url(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_chrome_url(tmp_path, "https://from-config")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_chrome = mocker.patch("jailbee.gui.open_chrome")

    result = CliRunner().invoke(app, ["chrome", "feat-x", "https://from-cli"])
    assert result.exit_code == 0, result.stdout
    assert open_chrome.call_args.args[3] == "https://from-cli"


def test_chrome_cmd_errors_when_chrome_disabled(tmp_path, mocker):
    """`gie chrome` errors with exit 2 when chrome.enabled is false."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text("chrome:\n  enabled: false\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_chrome = mocker.patch("jailbee.gui.open_chrome")

    result = CliRunner().invoke(app, ["chrome", "feat-x"])

    assert result.exit_code == 2
    open_chrome.assert_not_called()


def test_chrome_cmd_passes_none_when_no_url_anywhere(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo_with_chrome_url(tmp_path, None)
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch(
        "jailbee.cli._resolve_attachable",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    open_chrome = mocker.patch("jailbee.gui.open_chrome")

    result = CliRunner().invoke(app, ["chrome", "feat-x"])
    assert result.exit_code == 0, result.stdout
    assert open_chrome.call_args.args[3] is None


# --- Phase A Task 7: config show --layer -------------------------------------


def test_cli_config_show_layer_global_prints_user_yaml(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text("ide: pycharm\n")
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / ".config" / "jailbee").mkdir(parents=True)
    (tmp_path / ".config" / "jailbee" / "global.yaml").write_text("ide: idea\n")

    result = runner.invoke(app, ["config", "show", "--layer", "global"])

    assert result.exit_code == 0
    assert "ide: idea" in result.stdout
    assert "ide: pycharm" not in result.stdout


def test_cli_config_show_layer_repo_prints_repo_yaml(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text("ide: pycharm\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".config" / "jailbee").mkdir(parents=True)
    (tmp_path / ".config" / "jailbee" / "global.yaml").write_text("ide: idea\n")

    result = runner.invoke(app, ["config", "show", "--layer", "repo"])

    assert result.exit_code == 0
    assert "ide: pycharm" in result.stdout
    assert "ide: idea" not in result.stdout


def test_cli_config_show_layer_global_empty_when_missing(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text("ide: pycharm\n")
    (tmp_path / ".git").mkdir()

    result = runner.invoke(app, ["config", "show", "--layer", "global"])

    assert result.exit_code == 0
    # Empty YAML or `{}` — both acceptable; the key thing is no crash.
    assert "ide:" not in result.stdout


def test_cli_config_show_layer_effective_is_default(tmp_path, monkeypatch, mocker):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: myrepo\njetbrains:\n  ide: pycharm\n"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".config" / "jailbee").mkdir(parents=True)
    (tmp_path / ".config" / "jailbee" / "global.yaml").write_text(
        "egress_allow: [api.anthropic.com:443]\n"
    )
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    # Effective view has merged the global egress_allow into the (empty) repo list
    assert "api.anthropic.com" in result.stdout
    assert "ide: pycharm" in result.stdout


def test_cli_config_show_effective_reflects_claude_auto_egress(tmp_path, monkeypatch, mocker):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: myrepo\nclaude:\n  enabled: true\negress_allow: []\n"
    )
    (tmp_path / ".git").mkdir()
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "api.anthropic.com:443" in result.stdout
    assert "code.claude.com:443" in result.stdout


def test_cli_config_show_effective_reflects_claude_auto_shared_caches(
    tmp_path, monkeypatch, mocker
):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: myrepo\nclaude:\n  enabled: true\n"
    )
    (tmp_path / ".git").mkdir()
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "name: claude" in result.stdout
    assert "name: claude-json" not in result.stdout


def test_cli_config_show_repo_layer_does_not_inject_auto_entries(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: myrepo\nclaude:\n  enabled: true\negress_allow: []\n"
    )
    (tmp_path / ".git").mkdir()

    result = runner.invoke(app, ["config", "show", "--layer", "repo"])

    assert result.exit_code == 0
    assert "api.anthropic.com" not in result.stdout
    assert "code.claude.com" not in result.stdout


def test_cli_config_show_includes_resolved_agents(tmp_path, monkeypatch, mocker):
    """The override story requires seeing what a preset resolved to: a user
    enabling `agents.codex` must be able to see the preset's install command
    and egress host in `config show`, not just `enabled: true`."""
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: myrepo\nagents:\n  codex:\n    enabled: true\n"
    )
    (tmp_path / ".git").mkdir()
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "npm i -g @openai/codex" in result.stdout
    assert "api.openai.com:443" in result.stdout


def test_cli_config_show_agents_claude_keeps_subclass_fields_no_top_level_claude(
    tmp_path, monkeypatch, mocker
):
    """Pins a deliberate shape, closing a regression from an earlier task:
    `Config.claude` became a read-only property, so it no longer appears in
    `model_dump()` — settings now live only under `agents.claude`, and that
    entry must keep ClaudeAgentConfig's own fields (e.g. `plugins_enabled`),
    not just the fields shared with the generic `AgentConfig` base."""
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: myrepo\nagents:\n  claude:\n    enabled: true\n"
    )
    (tmp_path / ".git").mkdir()
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # Line-exact checks, not a full yaml.safe_load(result.stdout): the
    # leading "# Effective config (merged from global + <path>)" header is
    # printed through Rich and can word-wrap onto a second, un-commented
    # physical line for a long tmp_path, which breaks a whole-document parse
    # (pre-existing quirk of `info()`, unrelated to this change).
    raw_lines = result.stdout.splitlines()
    assert "claude:" not in raw_lines  # no top-level (unindented) `claude:` key
    stripped_lines = [line.strip() for line in raw_lines]
    assert "plugins_enabled: true" in stripped_lines
    assert "install_jailbee_skills: true" in stripped_lines


def test_cli_fetch_invokes_sync(mocker, tmp_path):
    from jailbee.sync import FetchResult

    runner = CliRunner()

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mock_fetch = mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=FetchResult(
            branch="feat/foo",
            old_oid="abc1234aa",
            new_oid="def5678bb",
            base_oid="abc1234aa",
            commits_added=2,
        ),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=["def5678 fix"])

    result = runner.invoke(app, ["git", "fetch", "feat-foo"])
    assert result.exit_code == 0, result.output
    mock_fetch.assert_called_once()
    assert "feat/foo" in result.output
    assert "2 new commits" in result.output


def test_cli_fetch_sync_error_exits_1(mocker, tmp_path):
    from jailbee.sync import SyncError

    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        side_effect=SyncError("not running"),
    )

    result = runner.invoke(app, ["git", "fetch", "feat-foo"])
    assert result.exit_code == 1
    assert "not running" in result.output


def test_cli_checkout_invokes_sync(mocker, tmp_path):
    runner = CliRunner()

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mock_co = mocker.patch("jailbee.sync.checkout_from_container")

    result = runner.invoke(app, ["git", "checkout", "feat-foo"])
    assert result.exit_code == 0, result.output
    mock_co.assert_called_once()


def test_cli_checkout_sync_error_exits_1(mocker, tmp_path):
    from jailbee.sync import SyncError

    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.checkout_from_container",
        side_effect=SyncError("Branch 'feat/foo' on host has diverged"),
    )

    result = runner.invoke(app, ["git", "checkout", "feat-foo"])
    assert result.exit_code == 1
    assert "diverged" in result.output


def test_cli_checkout_passes_as_name_through(mocker, tmp_path):
    runner = CliRunner()

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-compose-4", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="compose-4")
    mock_co = mocker.patch("jailbee.sync.checkout_from_container")

    result = runner.invoke(app, ["git", "checkout", "compose-4", "--as", "compose-4-1"])

    assert result.exit_code == 0, result.output
    assert mock_co.call_args.kwargs["as_name"] == "compose-4-1"


def test_cli_checkout_git_error_exits_1(mocker, tmp_path):
    """A git failure below the SyncError layer (e.g. `git fetch` exiting 128)
    must exit 1 with the message, not blow up as an unhandled traceback.
    """
    from jailbee.git import GitError

    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.checkout_from_container",
        side_effect=GitError("git fetch failed (exit 128)"),
    )

    result = runner.invoke(app, ["git", "checkout", "feat-foo"])

    assert result.exit_code == 1
    assert "exit 128" in result.output


def test_cli_fetch_git_error_exits_1(mocker, tmp_path):
    """Same for `gie git fetch` — it shares the failing code path."""
    from jailbee.git import GitError

    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        side_effect=GitError("git fetch failed (exit 128)"),
    )

    result = runner.invoke(app, ["git", "fetch", "feat-foo"])

    assert result.exit_code == 1
    assert "exit 128" in result.output


def test_cli_pull_invokes_sync(mocker, tmp_path):
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mock_merge = mocker.patch("jailbee.sync.merge_from_container")

    result = runner.invoke(app, ["git", "pull", "feat-foo"])
    assert result.exit_code == 0, result.output
    mock_merge.assert_called_once()


def test_cli_pull_git_error_exits_1(mocker, tmp_path):
    from jailbee.git import GitError

    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        side_effect=GitError("CONFLICT (content): foo.py"),
    )

    result = runner.invoke(app, ["git", "pull", "feat-foo"])
    assert result.exit_code == 1
    assert "CONFLICT" in result.output


def test_cli_checkout_prints_summary(mocker, tmp_path):
    from jailbee.sync import CheckoutResult, FetchResult

    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.checkout_from_container",
        return_value=CheckoutResult(
            fetch=FetchResult(
                branch="feat/foo",
                old_oid=None,
                new_oid="def5678def",
                base_oid="aaa0000aaa",
                commits_added=1,
            ),
            branch="feat/foo",
            head_oid="def5678def",
            created_new=True,
        ),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=["def5678 fix"])

    result = runner.invoke(app, ["git", "checkout", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "1 commit(s) ahead of HEAD" in result.output
    assert "Now on 'feat/foo' at def5678" in result.output


def _stub_pull_result(
    commits_added=1,
    branch="feat/foo",
    into_branch="main",
    pre_merge_head="aaaaaaaaaa",
    head_oid="f00ba12f00b",
):
    from jailbee.sync import FetchResult, MergeResult

    return MergeResult(
        fetch=FetchResult(
            branch=branch,
            old_oid="abc1234abc" if commits_added else None,
            new_oid="def5678def",
            base_oid="abc1234abc",
            commits_added=commits_added,
        ),
        branch=branch,
        head_oid=head_oid,
        into_branch=into_branch,
        pre_merge_head=pre_merge_head,
    )


def _stub_cleanup_result(
    *, destroyed=False, deleted_branch=False, cleanup_error=None, skipped_reason=None
):
    from jailbee.sync import CleanupResult

    return CleanupResult(
        destroyed=destroyed,
        deleted_branch=deleted_branch,
        cleanup_error=cleanup_error,
        skipped_reason=skipped_reason,
    )


def test_cli_pull_prints_summary(mocker, tmp_path):
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(),
    )
    mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=["def5678 fix"])

    result = runner.invoke(app, ["git", "pull", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "Merged 'feat/foo' from container 'feat-foo' into 'main'." in result.output
    assert "HEAD now at f00ba12" in result.output


def test_cli_pull_flag_forces_always(mocker, tmp_path):
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(),
    )
    mock_cleanup = mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(destroyed=True, deleted_branch=True),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=[])

    result = runner.invoke(app, ["git", "pull", "feat-foo", "--cleanup"])

    assert result.exit_code == 0, result.output
    assert mock_cleanup.call_args.kwargs.get("destroy_policy") == "always"
    assert mock_cleanup.call_args.kwargs.get("branch_policy") == "always"
    assert "Destroyed container 'feat-foo'." in result.output
    assert "Deleted local branch 'feat/foo'." in result.output


def test_cli_pull_prints_cleanup_after_summary(mocker, tmp_path):
    """The destroy / branch-delete lines must come AFTER the merge summary.

    The user wants to see what was merged before being asked
    whether to throw the container away. The CLI orders the messages,
    but we still verify it here so a future refactor doesn't reorder them.
    """
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(),
    )
    mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(destroyed=True),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=["def5678 fix"])

    result = runner.invoke(app, ["git", "pull", "feat-foo", "--cleanup"])

    assert result.exit_code == 0, result.output
    head_line = result.output.index("HEAD now at")
    destroyed_line = result.output.index("Destroyed container")
    assert head_line < destroyed_line


def test_cli_pull_warns_when_cleanup_skipped(mocker, tmp_path):
    """0-commits skip path: warn and hint at manual destroy."""
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(commits_added=0),
    )
    mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(
            skipped_reason="no new commits were merged — container kept"
        ),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=[])

    result = runner.invoke(app, ["git", "pull", "feat-foo", "--cleanup"])

    assert result.exit_code == 0, result.output
    assert "Skipping cleanup" in result.output
    assert "jailbee destroy feat-foo" in result.output


def test_cli_pull_cleanup_error_warns_but_exits_zero(mocker, tmp_path):
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(),
    )
    mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(cleanup_error="incus is on fire"),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=[])

    result = runner.invoke(app, ["git", "pull", "feat-foo", "--cleanup"])

    assert result.exit_code == 0
    assert "incus is on fire" in result.output


def test_cli_pull_no_cleanup_flag_forces_never(mocker, tmp_path):
    """--no-cleanup → both policies = 'never'."""
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    cfg_mock.pull.destroy_container = "always"
    cfg_mock.pull.delete_branch = "always"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(),
    )
    mock_cleanup = mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=[])

    result = runner.invoke(app, ["git", "pull", "feat-foo", "--no-cleanup"])

    assert result.exit_code == 0, result.output
    assert mock_cleanup.call_args.kwargs["destroy_policy"] == "never"
    assert mock_cleanup.call_args.kwargs["branch_policy"] == "never"


def test_cli_pull_cleanup_and_no_cleanup_are_mutually_exclusive(mocker, tmp_path):
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mock_merge = mocker.patch("jailbee.sync.merge_from_container")

    result = runner.invoke(app, ["git", "pull", "feat-foo", "--cleanup", "--no-cleanup"])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    mock_merge.assert_not_called()


def test_cli_pull_passes_config_policies_when_no_flag(mocker, tmp_path):
    """Without flags, the CLI passes cfg.pull.* through verbatim."""
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    cfg_mock.pull.destroy_container = "always"
    cfg_mock.pull.delete_branch = "never"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(),
    )
    mock_cleanup = mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=[])

    result = runner.invoke(app, ["git", "pull", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert mock_cleanup.call_args.kwargs["destroy_policy"] == "always"
    assert mock_cleanup.call_args.kwargs["branch_policy"] == "never"


def test_pull_forwards_into_and_checkout(mocker, tmp_path):
    """--into and --checkout are forwarded to sync.merge_from_container."""
    runner = CliRunner()
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-x", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_merge = mocker.patch(
        "jailbee.sync.merge_from_container",
        return_value=_stub_pull_result(branch="feat/x", into_branch="dev"),
    )
    mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=_stub_cleanup_result(),
    )
    mocker.patch("jailbee.git.log_oneline", return_value=[])

    result = runner.invoke(app, ["git", "pull", "feat-x", "--into", "dev", "--checkout"])

    assert result.exit_code == 0, result.output
    assert mock_merge.called
    _, kwargs = mock_merge.call_args
    assert kwargs["into"] == "dev"
    assert kwargs["allow_checkout"] is True
    assert "Now on 'dev'." in result.output


def test_pull_current_resolves_to_checked_out_branch(mocker, tmp_path):
    """--current passes the host's current branch as `into` to _do_single_pull."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.pull.destroy_container = "never"
    cfg_mock.pull.delete_branch = "never"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="myrepo-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch("jailbee.git.get_current_branch", return_value="dev")
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["git", "pull", "feat-foo", "--current"])

    assert result.exit_code == 0, result.output
    assert do_pull.call_args.kwargs["into"] == "dev"


def test_pull_current_and_into_are_mutually_exclusive(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.pull.destroy_container = "never"
    cfg_mock.pull.delete_branch = "never"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)

    result = CliRunner().invoke(app, ["git", "pull", "feat-foo", "--current", "--into", "dev"])

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "mutually exclusive" in combined.lower()


def test_pull_current_detached_head_errors(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.pull.destroy_container = "never"
    cfg_mock.pull.delete_branch = "never"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch("jailbee.git.get_current_branch", return_value=None)

    result = CliRunner().invoke(app, ["git", "pull", "feat-foo", "--current"])

    assert result.exit_code == 2
    # Rich soft-wraps the message at the console width, which depends on the
    # (environment-dependent) tmp_path length, so "detached HEAD" can straddle
    # a line break in CI. Collapse whitespace to make the check width-agnostic.
    combined = " ".join((result.stdout + (result.stderr or "")).split())
    assert "detached head" in combined.lower()


# ---- gie git retarget ----------------------------------------------------


def _retarget_cli_mocks(mocker, tmp_path):
    from jailbee.sync import RetargetResult

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-b"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-b")
    return mocker.patch(
        "jailbee.sync.retarget_container",
        return_value=RetargetResult(old_base="feat/a", new_base="main", base_oid="abc1234"),
    )


def test_git_retarget_happy_path_prints_hint(mocker, tmp_path):
    runner = CliRunner()
    mock_rt = _retarget_cli_mocks(mocker, tmp_path)

    result = runner.invoke(app, ["git", "retarget", "feat-b", "main"])

    assert result.exit_code == 0, result.output
    args, _ = mock_rt.call_args
    assert args[2] == "feat-b"
    assert args[3] == "main"
    assert "'feat/a' → 'main'" in result.output
    assert "jailbee git push feat-b --merge" in result.output


def test_git_retarget_merge_delegates_to_push(mocker, tmp_path):
    runner = CliRunner()
    _retarget_cli_mocks(mocker, tmp_path)
    push = mocker.patch("jailbee.cli._do_single_push", return_value="merged")

    result = runner.invoke(app, ["git", "retarget", "feat-b", "main", "--merge"])

    assert result.exit_code == 0, result.output
    _, kwargs = push.call_args
    assert kwargs["source"] == "main"
    assert kwargs["action"] == "merge"
    assert "gie git push feat-b --merge" not in result.output


def test_git_retarget_merge_failure_keeps_retarget_and_hints_retry(mocker, tmp_path):
    from jailbee.sync import SyncError

    runner = CliRunner()
    _retarget_cli_mocks(mocker, tmp_path)
    mocker.patch(
        "jailbee.cli._do_single_push",
        side_effect=SyncError("Container working tree is dirty."),
    )

    result = runner.invoke(app, ["git", "retarget", "feat-b", "main", "--merge"])

    assert result.exit_code == 1
    assert "'feat/a' → 'main'" in result.output  # retarget success still reported
    assert "dirty" in result.output  # merge error surfaced
    assert "re-run 'jailbee git push feat-b --merge'" in result.output


def test_git_retarget_sync_error_exits_1(mocker, tmp_path):
    from jailbee.sync import SyncError

    runner = CliRunner()
    mock_rt = _retarget_cli_mocks(mocker, tmp_path)
    mock_rt.side_effect = SyncError("container 'feat-b' is in mount mode")

    result = runner.invoke(app, ["git", "retarget", "feat-b", "main"])

    assert result.exit_code == 1
    assert "mount mode" in result.output


# ---- gie ls MODE column -------------------------------------------------


def test_ls_shows_ttl_column_only_when_loose_container_exists(tmp_path, mocker):
    """The TTL column is hidden when no container is in loose mode, and
    shown otherwise — both for containers with a TTL and with --no-revert."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    # Pass 1: only strict containers → no TTL column.
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {"user.jailbee.mode": "clone"},
        },
    ]
    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0, result.stdout
    assert "TTL" not in result.stdout

    # Pass 2: one loose container with TTL → column shown.
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    expiry = (now + timedelta(minutes=2, seconds=30)).isoformat()
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-debug",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
            "state": None,
            "config": {"user.jailbee.mode": "clone", "user.jailbee.loose_until": expiry},
        },
    ]
    mocker.patch("jailbee.cli._now", return_value=now)

    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0, result.stdout
    assert "TTL" in result.stdout
    assert "2m" in result.stdout


def test_ls_ttl_column_shows_dash_for_no_revert(tmp_path, mocker):
    """A loose container without `loose_until` (i.e. --no-revert) shows "—"."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-bug-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
            "state": None,
            "config": {},
        },
    ]
    incus_mock.return_value.config_get.side_effect = lambda name, key: (
        "clone" if key == "user.jailbee.mode" else None
    )

    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0, result.stdout
    assert "TTL" in result.stdout
    assert "—" in result.stdout


def test_net_status_shows_loose_ttl_section(tmp_path, mocker):
    """`gie net status` renders the Auto-revert section per loose container."""
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session, create_engine

    from jailbee.db import _ensure_schema

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    # Empty in-memory DB for the status command.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    _ensure_schema(engine)
    mocker.patch("jailbee.db.get_engine", return_value=engine)
    Session  # noqa: B018 — re-export sanity check

    # systemctl is-active → "active"
    mocker.patch(
        "subprocess.run",
        return_value=mocker.Mock(stdout="active\n", returncode=0),
    )

    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    expiry = (now + timedelta(minutes=2, seconds=14)).isoformat()

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-debug",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
            "state": None,
            "config": {"user.jailbee.mode": "clone", "user.jailbee.loose_until": expiry},
        },
        {
            "name": "myrepo-bug-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
            "state": None,
            "config": {"user.jailbee.mode": "clone"},
        },
    ]
    mocker.patch("jailbee.cli._now", return_value=now)

    result = CliRunner().invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    assert "Auto-revert" in result.stdout
    assert "feat-debug" in result.stdout
    assert "expires in 2m" in result.stdout
    assert "→ strict" in result.stdout
    assert "no expiry" in result.stdout
    assert "--no-revert" in result.stdout


def test_net_status_lists_port_forwards(tmp_path, mocker):
    """`jailbee net status` names every active forward and says they bypass the ACL."""
    from sqlmodel import create_engine

    from jailbee.db import _ensure_schema

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    _ensure_schema(engine)
    mocker.patch("jailbee.db.get_engine", return_value=engine)
    mocker.patch(
        "subprocess.run",
        return_value=mocker.Mock(stdout="active\n", returncode=0),
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {"user.jailbee.mode": "clone"},
            "devices": {
                "port-cfg-adb": {
                    "type": "proxy",
                    "bind": "instance",
                    "listen": "tcp:127.0.0.1:5037",
                    "connect": "tcp:127.0.0.1:5037",
                },
            },
        },
    ]

    result = CliRunner().invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout + (result.stderr or "")
    assert "Port forwards: 1 on 1 container(s) — the network ACL does not see these" in out
    assert "feat-x" in out
    assert "to-container" in out
    assert "127.0.0.1:5037" in out
    assert "(config)" in out


def test_net_status_omits_the_forward_section_when_there_are_none(tmp_path, mocker):
    """No forwards → no section at all, rather than an empty header."""
    from sqlmodel import create_engine

    from jailbee.db import _ensure_schema

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    _ensure_schema(engine)
    mocker.patch("jailbee.db.get_engine", return_value=engine)
    mocker.patch(
        "subprocess.run",
        return_value=mocker.Mock(stdout="active\n", returncode=0),
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {"user.jailbee.mode": "clone"},
            "devices": {"root": {"type": "disk", "path": "/"}},
        },
    ]

    result = CliRunner().invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    assert "Port forwards" not in result.stdout


def test_git_diff_invokes_committed_by_default(tmp_path, mocker):
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat"),
    )
    diff_mock = mocker.patch(
        "jailbee.sync.diff_from_container",
        return_value="DIFF OUTPUT",
    )

    result = CliRunner().invoke(app, ["git", "diff", "feat"])
    assert result.exit_code == 0, result.output
    assert "DIFF OUTPUT" in result.output

    _, kwargs = diff_mock.call_args
    assert kwargs["mode"] == "committed"
    assert kwargs["stat_only"] is False


def test_git_diff_wt_flag_sets_mode(tmp_path, mocker):
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat"),
    )
    diff_mock = mocker.patch(
        "jailbee.sync.diff_from_container",
        return_value="",
    )

    CliRunner().invoke(app, ["git", "diff", "feat", "--wt"])
    assert diff_mock.call_args.kwargs["mode"] == "wt"


def test_git_diff_wt_and_all_are_mutually_exclusive(tmp_path, mocker):
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat"),
    )

    result = CliRunner().invoke(app, ["git", "diff", "feat", "--wt", "--all"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_git_diff_stat_flag_passes_through(tmp_path, mocker):
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat"),
    )
    diff_mock = mocker.patch(
        "jailbee.sync.diff_from_container",
        return_value="",
    )

    CliRunner().invoke(app, ["git", "diff", "feat", "--stat"])
    assert diff_mock.call_args.kwargs["stat_only"] is True


def test_git_diff_color_flag_overrides_tty_detection(tmp_path, mocker):
    """`jailbee dashboard` pipes the diff into a pager, which makes stdout a
    pipe and would silence the colour the pager is there to render."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat"),
    )
    diff_mock = mocker.patch("jailbee.sync.diff_from_container", return_value="")

    CliRunner().invoke(app, ["git", "diff", "feat", "--color"])
    assert diff_mock.call_args.kwargs["color"] is True

    CliRunner().invoke(app, ["git", "diff", "feat", "--no-color"])
    assert diff_mock.call_args.kwargs["color"] is False


def test_git_diff_without_the_flag_follows_stdout(tmp_path, mocker):
    """CliRunner's stdout is not a TTY, so the default must resolve to False —
    the behaviour every existing caller already had."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat"),
    )
    diff_mock = mocker.patch("jailbee.sync.diff_from_container", return_value="")

    CliRunner().invoke(app, ["git", "diff", "feat"])
    assert diff_mock.call_args.kwargs["color"] is False


def test_ls_renders_base_and_git_columns(tmp_path, mocker):
    """`gie ls` table includes BASE / WT / AHEAD ± / ↑ columns."""
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import ContainerInfo

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    containers = [
        ContainerInfo(
            name="myrepo-feat-a",
            state="Running",
            network="strict",
            ip="10.0.0.42",
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch="main",
            git_status=GitStatus(
                wt="+12 -3", ahead_diff="+245 -18", ahead_count="3", conflict="ok"
            ),
        ),
        ContainerInfo(
            name="myrepo-legacy",
            state="Stopped",
            network="strict",
            ip=None,
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch=None,
            git_status=None,
        ),
    ]
    mocker.patch("jailbee.lifecycle.list_containers", return_value=containers)
    mocker.patch("jailbee.incus.Incus")

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout

    out = result.stdout
    assert "BASE" in out
    assert "WT" in out
    assert "AHEAD" in out
    assert "main" in out  # base for feat-a
    assert "+12 -3" in out  # WT for feat-a
    assert "+245 -18" in out  # AHEAD ± for feat-a
    assert "—" in out  # legacy row has dashes


def test_ls_renders_merge_conflict_column_table(tmp_path, mocker):
    """`gie ls` table includes a MERGE column showing conflict state."""

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import ContainerInfo

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    containers = [
        ContainerInfo(
            name="myrepo-feat-conflict",
            state="Running",
            network="strict",
            ip="10.0.0.1",
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch="main",
            git_status=GitStatus(
                wt="clean", ahead_diff="+1 -0", ahead_count="1", conflict="conflict"
            ),
        ),
        ContainerInfo(
            name="myrepo-feat-ok",
            state="Running",
            network="strict",
            ip="10.0.0.2",
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch="main",
            git_status=GitStatus(wt="clean", ahead_diff="+0 -0", ahead_count="0", conflict="ok"),
        ),
        ContainerInfo(
            name="myrepo-legacy",
            state="Stopped",
            network="strict",
            ip=None,
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch=None,
            git_status=None,
        ),
    ]
    mocker.patch("jailbee.lifecycle.list_containers", return_value=containers)
    mocker.patch("jailbee.incus.Incus")

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.stdout

    out = result.stdout
    assert "MERGE" in out  # column header
    assert "conflict" in out  # value for the conflict container
    assert "ok" in out  # value for the ok container


def test_ls_merge_conflict_in_git_status_json(tmp_path, mocker):
    """`gie ls --format json --fields name,git_status` includes `conflict` key."""
    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import GitStatus

    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-feat-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            # See the note in the --fields test: without `user.jailbee.repo_dir`
            # this container is not a probe target and the stub is never used.
            "config": {"user.jailbee.repo_dir": "/home/dev/myrepo"},
        },
    ]
    incus_mock.return_value.config_get.return_value = None
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={
            "myrepo-feat-x": GitStatus(
                wt="clean", ahead_diff="+1 -0", ahead_count="1", conflict="conflict"
            ),
        },
    )

    result = CliRunner().invoke(
        app,
        ["ls", "--format", "json", "--fields", "name,git_status"],
    )
    assert result.exit_code == 0, result.stdout
    data = _json.loads(result.stdout)
    assert data == [
        {
            "name": "feat-x",
            "git_status": {
                "wt": "clean",
                "ahead_diff": "+1 -0",
                "ahead_count": "1",
                "conflict": "conflict",
                "head_sha": "",
                "remote_contained": None,
                "local_diff": "?",
                "local_count": "?",
            },
        }
    ]


def test_ls_shows_mode_column(tmp_path, mocker):
    """`gie ls` table includes a MODE column showing clone/mount."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )

    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "myrepo-clonefoo",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {"user.jailbee.mode": "clone"},
        },
        {
            "name": "myrepo-mountfoo",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {"user.jailbee.mode": "mount"},
        },
    ]

    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0, result.stdout
    assert "MODE" in result.stdout
    assert "clone" in result.stdout
    assert "mount" in result.stdout


# ---- gie new --mount ----------------------------------------------------


def test_new_mount_rejects_base(tmp_path, mocker):
    """`gie new myname main --mount` exits 2 with a base-mode message."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_global")

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    result = CliRunner().invoke(app, ["new", "myname", "main", "--mount"])

    assert result.exit_code == 2
    assert "base" in result.output.lower()
    new_container.assert_not_called()


def test_new_mount_rejects_no_clone(tmp_path, mocker):
    """`gie new myname --mount --no-clone` exits 2 with a redundancy message."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_global")

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    result = CliRunner().invoke(app, ["new", "myname", "--mount", "--no-clone"])

    assert result.exit_code == 2
    assert "redundant" in result.output.lower() or "no-clone" in result.output.lower()
    new_container.assert_not_called()


def test_new_mount_rejects_slash_in_name(tmp_path, mocker):
    """`gie new feat/x --mount` exits 2 — positional is a name, not a branch."""
    repo = _setup_repo(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_global")

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    result = CliRunner().invoke(app, ["new", "feat/x", "--mount"])

    assert result.exit_code == 2
    assert "container name" in result.output.lower() or "feat/x" in result.output
    new_container.assert_not_called()


def test_new_without_mount_requires_git_repo(tmp_path, mocker):
    """`gie new feat-x` on a non-git repo_root exits 2 and points to --mount."""
    repo = _setup_repo(tmp_path, "myrepo")
    # Strip .git to simulate non-git directory
    import shutil

    shutil.rmtree(repo / ".git")

    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_global")

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    result = CliRunner().invoke(app, ["new", "feat-x"])

    assert result.exit_code == 2
    assert "git" in result.output.lower()
    assert "--mount" in result.output
    new_container.assert_not_called()


def test_new_with_mount_works_without_git_repo(tmp_path, mocker):
    """`gie new myname --mount` succeeds even when repo_root has no .git."""
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _setup_repo(tmp_path, "myrepo")
    import shutil

    shutil.rmtree(repo / ".git")

    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    gcfg = GlobalConfig(docker_registry_mirror=DockerRegistryMirror(enabled=False))
    mocker.patch("jailbee.cli._load_global", return_value=gcfg)

    new_container = mocker.patch(
        "jailbee.lifecycle.new_container",
        return_value="myrepo-myname",
    )
    result = CliRunner().invoke(app, ["new", "myname", "--mount", "--no-autostart"])

    assert result.exit_code == 0, result.output
    new_container.assert_called_once()
    opts = new_container.call_args.args[2]
    assert opts.mount is True
    assert opts.clone is False
    assert opts.base is None
    assert opts.name == "myrepo-myname"


def test_cli_new_help_shows_pr_example() -> None:
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["new", "--help"])
    assert result.exit_code == 0
    assert "--pr 1234" in result.stdout


def test_cli_push_transport_only(mocker, tmp_path):
    """`gie git push feat-x` calls push_to_container with no merge/rebase follow-up."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/heads/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="newoid",
        ),
    )
    mock_merge = mocker.patch("jailbee.sync.push_and_merge")
    mock_rebase = mocker.patch("jailbee.sync.push_and_rebase")

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    mock_push.assert_called_once()
    mock_merge.assert_not_called()
    mock_rebase.assert_not_called()


def test_cli_push_merge_conflict_prints_submodule_report(mocker, tmp_path):
    """`git push --merge` renders the same submodule block the pull path does."""
    from typer.testing import CliRunner

    from jailbee import submodules, sync
    from jailbee.cli import app

    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    report = sync.ConflictReport(
        resolution=submodules.GitlinkResolution(
            resolved=["deps/libfoo"],
            unresolved=[submodules.UnresolvedSub("vendor/baz", "content-conflict", "")],
        ),
        nongitlink=[],
        branch="main",
        location="jailbee shell feat-x",
    )
    mocker.patch(
        "jailbee.sync.push_and_merge",
        side_effect=sync.MergeConflictError("conflicts", report=report),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--merge"])

    assert result.exit_code == 1
    assert "deps/libfoo" in result.output
    assert "vendor/baz" in result.output


def test_cli_push_with_merge(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import MergeInContainerResult, PushResult

    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid=None,
        new_oid="newoid",
    )
    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_push = mocker.patch("jailbee.sync.push_to_container")
    mock_merge = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=push_result,
            container_branch="feat-x",
            fast_forward_only=False,
            head_oid="container-head",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--merge"])

    assert result.exit_code == 0, result.output
    mock_push.assert_not_called()
    mock_merge.assert_called_once()


def test_cli_push_with_rebase(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult, RebaseInContainerResult

    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid=None,
        new_oid="newoid",
    )
    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_rebase = mocker.patch(
        "jailbee.sync.push_and_rebase",
        return_value=RebaseInContainerResult(
            push=push_result,
            container_branch="feat-x",
            head_oid="container-head",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--rebase"])

    assert result.exit_code == 0, result.output
    mock_rebase.assert_called_once()


def test_cli_push_merge_and_rebase_together_errors(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch(
        "jailbee.cli._load_or_exit",
        return_value=_push_cfg_factory(action="plain", source="default-branch"),
    )
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--merge", "--rebase"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_cli_push_force_and_merge_together_errors(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch(
        "jailbee.cli._load_or_exit",
        return_value=_push_cfg_factory(action="plain", source="default-branch"),
    )
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--force", "--merge"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_cli_push_force_requires_name(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch(
        "jailbee.cli._load_or_exit",
        return_value=_push_cfg_factory(action="plain", source="default-branch"),
    )

    result = CliRunner().invoke(app, ["git", "push", "--force"])

    assert result.exit_code == 2
    assert "explicit container name" in result.output.lower()


def test_resolve_push_action_force_wins():
    from jailbee.cli import _resolve_push_action

    cfg = _push_cfg_factory(action="merge", source="default-branch")
    action = _resolve_push_action(
        cfg, merge_flag=False, rebase_flag=False, plain_flag=False, force_flag=True
    )
    assert action == "force"


def test_cli_push_with_force_invokes_push_and_reset(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult, ResetInContainerResult

    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid="oldoid0",
        new_oid="newoid0",
    )
    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_reset = mocker.patch(
        "jailbee.sync.push_and_reset",
        return_value=ResetInContainerResult(
            push=push_result,
            container_branch="main",
            head_oid="newhead",
            discarded_commits=2,
            old_branch_oid="doomedoid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--force"])

    assert result.exit_code == 0, result.output
    mock_reset.assert_called_once()
    assert "discarded 2" in result.output.lower()
    assert "(was doomedo" in result.output.lower()


def test_cli_push_force_no_discard_omits_warning(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult, ResetInContainerResult

    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid="oldoid0",
        new_oid="newoid0",
    )
    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.sync.push_and_reset",
        return_value=ResetInContainerResult(
            push=push_result,
            container_branch="main",
            head_oid="newhead0",
            discarded_commits=0,
            old_branch_oid="oldhead0",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--force"])

    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "reset 'main' to" in out
    assert "head now at newhead" in out
    # Nothing discarded -> no warning line.
    assert "discarded" not in out
    assert "⚠" not in result.output


def test_cli_push_from_propagates(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    mocker.patch(
        "jailbee.cli._load_or_exit",
        return_value=_push_cfg_factory(action="plain", source="default-branch"),
    )
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="dev",
            source_ref="refs/heads/dev",
            container_ref="refs/jailbee/host/dev",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--from", "dev"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source") == "dev"


def test_cli_push_sync_error_exit_non_zero(mocker, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import SyncError

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.sync.push_to_container",
        side_effect=SyncError("Container is not running"),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 1


def test_cli_push_git_error_exit_non_zero(mocker, tmp_path):
    """A git failure under the push (submodule transport, receive-pack, …)
    must exit 1 with the message, not surface as an unhandled traceback.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git import GitError

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.sync.push_to_container",
        side_effect=GitError("git push failed (exit 128)"),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 1
    assert "exit 128" in result.output


# --- gie net loose / strict label lifecycle ---------------------------------


def _setup_net_test(tmp_path, mocker, *, cfg_overrides=None, gcfg=None, pre_mode="strict"):
    """Shared setup for net_loose/strict tests.

    Returns the mocked Incus instance so the test can inspect calls.
    """
    from datetime import UTC, datetime

    from jailbee import cli
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, **(cfg_overrides or {}))
    if gcfg is None:
        gcfg = GlobalConfig(
            loose_auto_revert=LooseAutoRevert(enabled=True, after="5m"),
        )

    mocker.patch.object(cli, "_load_or_exit", return_value=cfg)
    mocker.patch.object(cli, "_load_global", return_value=gcfg)

    incus = mocker.Mock(spec=Incus)
    incus.config_get.return_value = None
    mocker.patch.object(
        cli,
        "_resolve_existing",
        return_value=(incus, f"{cfg.container_prefix}-feat-x"),
    )
    mocker.patch.object(cli, "_mirror_endpoint_or_none", return_value=None)
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value=pre_mode,
    )
    mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch(
        "jailbee.cli._now",
        return_value=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
    )
    return incus, cfg


def test_net_loose_sets_loose_until_and_revert_to(tmp_path, mocker):
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")
    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])
    assert result.exit_code == 0, result.stdout

    set_calls = incus.config_set.call_args_list
    keys = {c.args[1]: c.args[2] for c in set_calls}
    assert keys["user.jailbee.loose_revert_to"] == "strict"
    assert keys["user.jailbee.loose_until"] == "2026-05-20T12:05:00+00:00"


def test_net_loose_no_revert_clears_labels(tmp_path, mocker):
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")
    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--no-revert"])
    assert result.exit_code == 0, result.stdout

    assert incus.config_set.call_count == 0  # no label sets
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys
    assert "user.jailbee.loose_revert_to" in unset_keys


def test_net_strict_clears_loose_labels(tmp_path, mocker):
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="loose")
    result = CliRunner().invoke(app, ["net", "strict", "feat-x"])
    assert result.exit_code == 0, result.stdout

    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys
    assert "user.jailbee.loose_revert_to" in unset_keys


def test_net_strict_warns_when_a_wanted_mirror_is_unavailable(tmp_path, mocker):
    """`jailbee new --network loose` with the mirror down succeeds by design,
    so `net strict` is where a Docker repo first meets a broken dockerd — and
    the only place that can still name the remedy."""
    _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"golden": {"stacks": {"docker": True}}},
        pre_mode="loose",
    )
    result = CliRunner().invoke(app, ["net", "strict", "feat-x"])

    assert result.exit_code == 0, result.stdout
    # Rich hard-wraps the warning, so compare on collapsed whitespace.
    flat = " ".join(result.stdout.split())
    assert "Registry mirror unavailable" in flat
    assert "jailbee registry up && jailbee apply" in flat


def test_net_strict_is_silent_when_the_repo_does_not_want_the_mirror(tmp_path, mocker):
    """The warning must be gated on `mirror_wanted`, not on the endpoint alone,
    or every non-Docker repo gets it on every `net strict`."""
    _setup_net_test(tmp_path, mocker, pre_mode="loose")
    result = CliRunner().invoke(app, ["net", "strict", "feat-x"])

    assert result.exit_code == 0, result.stdout
    assert "registry up" not in result.stdout


def test_net_loose_already_loose_preserves_revert_to(tmp_path, mocker):
    """If container is already loose, keep the existing revert_to value."""
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="loose")
    incus.config_get.side_effect = lambda name, key: (
        "strict" if key == "user.jailbee.loose_revert_to" else None
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])
    assert result.exit_code == 0, result.stdout

    set_keys = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert set_keys["user.jailbee.loose_revert_to"] == "strict"


def test_net_loose_policy_disabled_sets_no_labels(tmp_path, mocker):
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig

    incus, _ = _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"loose_auto_revert": {"enabled": False}},
        gcfg=GlobalConfig(loose_auto_revert=LooseAutoRevert(enabled=False)),
        pre_mode="strict",
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])
    assert result.exit_code == 0, result.stdout
    assert incus.config_set.call_count == 0  # no label sets when disabled


def test_net_loose_no_revert_survives_malformed_policy(tmp_path, mocker):
    """--no-revert must not evaluate the config policy's duration() at all.

    A config with an out-of-range `loose_auto_revert.after` (e.g. `30h`,
    over the 24h cap) is still a valid config to load — only `.duration()`
    validates it. Before this task, `--no-revert` never called
    `.duration()` in the first place, so a malformed policy was harmless
    as long as the user always passed --no-revert. That must keep working.
    """
    incus, _ = _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"loose_auto_revert": {"after": "30h"}},
        pre_mode="strict",
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--no-revert"])

    assert result.exit_code == 0, result.stdout
    assert incus.config_set.call_count == 0
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys
    assert "user.jailbee.loose_revert_to" in unset_keys


def test_net_loose_unparseable_policy_errors_before_touching_the_network(tmp_path, mocker):
    """A malformed policy must fail the command, not the switch half-way.

    `after` is only validated by `.duration()`, so `30min` loads as a
    perfectly valid config and breaks at the first thing that parses it.
    That used to be `_switch`, *after* `switch_network` had already run —
    leaving the container in loose with no TTL label and an uncaught
    `ValueError` traceback in the user's face.
    """
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    incus, _ = _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"loose_auto_revert": {"after": "30min"}},
        pre_mode="strict",
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 2, result.stdout
    assert "loose_auto_revert.after" in result.output
    assert "30min" in result.output
    assert switch.call_count == 0
    assert incus.config_set.call_count == 0
    assert incus.config_unset.call_count == 0


def test_net_loose_unparseable_policy_is_never_offered_as_a_prompt_default(tmp_path, mocker):
    """The prompt's default comes from the same unvalidated config field.

    Offering it meant the user pressed Enter on `30min  (config default)`
    and got the parse error as a traceback from inside the prompt.
    """
    _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"loose_auto_revert": {"after": "30min"}},
        pre_mode="strict",
    )
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_loose_ttl")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 2, result.stdout
    prompt.assert_not_called()


def test_net_loose_for_flag_bypasses_an_unparseable_policy(tmp_path, mocker):
    """--for decides the TTL itself, so the broken policy is never read."""
    incus, _ = _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"loose_auto_revert": {"after": "30min"}},
        pre_mode="strict",
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "2h"])

    assert result.exit_code == 0, result.stdout
    keys = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert keys["user.jailbee.loose_until"] == "2026-05-20T14:00:00+00:00"


def test_net_loose_for_flag_sets_custom_ttl(tmp_path, mocker):
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "2h"])

    assert result.exit_code == 0, result.stdout
    keys = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert keys["user.jailbee.loose_until"] == "2026-05-20T14:00:00+00:00"
    assert keys["user.jailbee.loose_revert_to"] == "strict"


def test_net_loose_for_never_clears_labels(tmp_path, mocker):
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "never"])

    assert result.exit_code == 0, result.stdout
    assert incus.config_set.call_count == 0
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys
    assert "user.jailbee.loose_revert_to" in unset_keys


def test_net_loose_for_and_no_revert_are_mutually_exclusive(tmp_path, mocker):
    _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "2h", "--no-revert"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_net_loose_for_rejects_unparseable_duration(tmp_path, mocker):
    _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "banana"])

    assert result.exit_code == 2
    assert "invalid duration" in result.output


def test_net_loose_for_rejects_over_24h(tmp_path, mocker):
    _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "25h"])

    assert result.exit_code == 2
    assert "24h" in result.output


def test_net_loose_prompts_when_interactive(tmp_path, mocker):
    """No --for and a TTY → ask, and use the answer as the TTL."""
    from datetime import timedelta

    from jailbee.cli import _LooseTtl

    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    prompt = mocker.patch(
        "jailbee.cli._prompt_loose_ttl",
        return_value=_LooseTtl(duration=timedelta(hours=3)),
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 0, result.stdout
    prompt.assert_called_once_with("5m")
    keys = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert keys["user.jailbee.loose_until"] == "2026-05-20T15:00:00+00:00"


def test_net_loose_prompt_cancel_aborts_without_switching(tmp_path, mocker):
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.cli._prompt_loose_ttl", return_value=None)

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code != 0
    assert incus.config_set.call_count == 0


def test_net_loose_does_not_prompt_with_for_flag(tmp_path, mocker):
    _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_loose_ttl")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "1h"])

    assert result.exit_code == 0, result.stdout
    prompt.assert_not_called()


def test_net_loose_does_not_prompt_with_no_revert(tmp_path, mocker):
    _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_loose_ttl")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--no-revert"])

    assert result.exit_code == 0, result.stdout
    prompt.assert_not_called()


def test_net_loose_does_not_prompt_when_policy_disabled(tmp_path, mocker):
    """Nothing to ask about when auto-revert is switched off in config."""
    from jailbee.config import LooseAutoRevert
    from jailbee.global_config import GlobalConfig

    _setup_net_test(
        tmp_path,
        mocker,
        cfg_overrides={"loose_auto_revert": {"enabled": False}},
        gcfg=GlobalConfig(loose_auto_revert=LooseAutoRevert(enabled=False)),
        pre_mode="strict",
    )
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_loose_ttl")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 0, result.stdout
    prompt.assert_not_called()


def test_net_loose_does_not_prompt_without_a_tty(tmp_path, mocker):
    """The Qt dashboard's detached Popen and any script land here."""
    incus, _ = _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    prompt = mocker.patch("jailbee.cli._prompt_loose_ttl")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 0, result.stdout
    prompt.assert_not_called()
    keys = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert keys["user.jailbee.loose_until"] == "2026-05-20T12:05:00+00:00"


def test_net_loose_with_for_does_not_read_the_global_config(tmp_path, mocker):
    """`--for` decides the TTL on its own, so the policy is never consulted."""
    from jailbee import cli

    _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--for", "2h"])

    assert result.exit_code == 0, result.stdout
    cli._load_global.assert_not_called()


def test_net_loose_with_no_revert_does_not_read_the_global_config(tmp_path, mocker):
    from jailbee import cli

    _setup_net_test(tmp_path, mocker, pre_mode="strict")

    result = CliRunner().invoke(app, ["net", "loose", "feat-x", "--no-revert"])

    assert result.exit_code == 0, result.stdout
    cli._load_global.assert_not_called()


def test_net_loose_reads_the_global_config_once_when_prompting(tmp_path, mocker):
    """The prompt default and the fallback TTL share one resolved policy."""
    from datetime import timedelta

    from jailbee import cli
    from jailbee.cli import _LooseTtl

    _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.cli._prompt_loose_ttl",
        return_value=_LooseTtl(duration=timedelta(hours=3)),
    )

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 0, result.stdout
    assert cli._load_global.call_count == 1


def test_net_loose_reads_the_global_config_once_without_a_tty(tmp_path, mocker):
    from jailbee import cli

    _setup_net_test(tmp_path, mocker, pre_mode="strict")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = CliRunner().invoke(app, ["net", "loose", "feat-x"])

    assert result.exit_code == 0, result.stdout
    assert cli._load_global.call_count == 1


def test_validate_duration_answer_returns_message_on_bad_input() -> None:
    from jailbee.cli import _validate_duration_answer

    assert _validate_duration_answer("2h") is True
    assert _validate_duration_answer("never") is True
    assert isinstance(_validate_duration_answer("banana"), str)
    assert isinstance(_validate_duration_answer("25h"), str)


def test_prompt_loose_ttl_preset_answer_returns_parsed_duration(mocker) -> None:
    """Selecting a preset like "2h" must parse through to a real timedelta."""
    from datetime import timedelta

    from jailbee.cli import _LooseTtl, _prompt_loose_ttl

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = "2h"

    result = _prompt_loose_ttl("5m")

    assert result == _LooseTtl(duration=timedelta(hours=2))


def test_prompt_loose_ttl_never_answer_is_ttl_with_no_duration_not_none(mocker) -> None:
    """ "never" means "no auto-revert", a *chosen* `_LooseTtl(duration=None)` —
    it must not be confused with the `None` sentinel used for cancellation."""
    from jailbee.cli import _LooseTtl, _prompt_loose_ttl

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = "never"

    result = _prompt_loose_ttl("5m")

    assert result is not None
    assert result == _LooseTtl(duration=None)


def test_prompt_loose_ttl_cancel_at_select_returns_none(mocker) -> None:
    """Ctrl-C at the select prompt — questionary's `.ask()` returns None."""
    from jailbee.cli import _prompt_loose_ttl

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = None

    result = _prompt_loose_ttl("5m")

    assert result is None


def test_prompt_loose_ttl_cancel_entry_aborts_instead_of_parsing_its_title(mocker) -> None:
    """Picking the "cancel" entry must abort, not fall through to the parser.

    The value is resolved from the real choice list by title, exactly like the
    "custom…" tests: `questionary.Choice` treats `value=None` as *unset* and
    substitutes the title, so a cancel entry built with `value=None` actually
    answers the string "cancel" — and a test that hardcoded the sentinel would
    sail straight past that.
    """
    from jailbee.cli import _prompt_loose_ttl

    select_mock = mocker.patch("questionary.select")

    def fake_select(_message, choices, default):
        cancel_choice = next(c for c in choices if c.title == "cancel")
        select_mock.return_value.ask.return_value = cancel_choice.value
        return select_mock.return_value

    select_mock.side_effect = fake_select

    assert _prompt_loose_ttl("5m") is None


def test_prompt_loose_ttl_custom_opens_text_prompt_and_parses_answer(mocker) -> None:
    """Picking "custom…" must hand off to questionary.text, validated by
    `_validate_duration_answer`, and the typed answer must be parsed through
    `config.parse_loose_ttl` (not swallowed or re-parsed some other way)."""
    from datetime import timedelta

    from jailbee.cli import _LooseTtl, _prompt_loose_ttl, _validate_duration_answer

    select_mock = mocker.patch("questionary.select")
    text_mock = mocker.patch("questionary.text")

    def fake_select(_message, choices, default):
        # Drive the real choice list built by _prompt_loose_ttl: find the
        # "custom…" entry and answer with its actual value, rather than
        # hardcoding the private sentinel string.
        custom_choice = next(c for c in choices if c.title == "custom…")
        select_mock.return_value.ask.return_value = custom_choice.value
        return select_mock.return_value

    select_mock.side_effect = fake_select
    text_mock.return_value.ask.return_value = "90m"

    result = _prompt_loose_ttl("5m")

    assert result == _LooseTtl(duration=timedelta(minutes=90))
    text_mock.assert_called_once()
    assert text_mock.call_args.kwargs["validate"] is _validate_duration_answer


def test_prompt_loose_ttl_custom_cancel_returns_none(mocker) -> None:
    from jailbee.cli import _prompt_loose_ttl

    select_mock = mocker.patch("questionary.select")
    text_mock = mocker.patch("questionary.text")

    def fake_select(_message, choices, default):
        custom_choice = next(c for c in choices if c.title == "custom…")
        select_mock.return_value.ask.return_value = custom_choice.value
        return select_mock.return_value

    select_mock.side_effect = fake_select
    text_mock.return_value.ask.return_value = None

    result = _prompt_loose_ttl("5m")

    assert result is None


def test_prompt_loose_ttl_inserts_missing_default_as_first_choice(mocker) -> None:
    """A `default_after` outside LOOSE_TTL_PRESETS (e.g. a custom config
    value like "7m") must still appear in the offered choices — and as the
    `default=` kwarg passed to questionary.select, so it can never trigger
    questionary's own ValueError for a default absent from `choices`."""
    from jailbee.cli import _prompt_loose_ttl

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = None  # cancel; only args matter here

    _prompt_loose_ttl("7m")

    select_mock.assert_called_once()
    _, kwargs = select_mock.call_args
    assert kwargs["default"] == "7m"
    choices = kwargs["choices"]
    values = [c.value for c in choices]
    assert values[0] == "7m"  # inserted at the front
    titles_by_value = {c.value: c.title for c in choices}
    assert titles_by_value["7m"] == "7m  (config default)"
    assert kwargs["default"] in values


def test_net_offline_command_is_gone() -> None:
    result = CliRunner().invoke(app, ["net", "offline", "feat-x"])
    assert result.exit_code != 0
    assert "No such command" in result.output or "Usage" in result.output


def test_new_rejects_offline_net(tmp_path, mocker):
    from jailbee import cli
    from tests.conftest import make_cfg

    mocker.patch.object(cli, "_load_or_exit", return_value=make_cfg(tmp_path))

    result = CliRunner().invoke(app, ["new", "feat-x", "--net", "offline"])

    assert result.exit_code == 2
    assert "was removed" in result.output


def test_cli_push_current_resolves_host_branch(mocker, tmp_path):
    """--current calls get_current_branch and passes the result as source."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    mocker.patch("jailbee.cli._load_or_exit", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.git.get_current_branch",
        return_value="feat/my-current",
    )
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/my-current",
            source_ref="refs/heads/feat/my-current",
            container_ref="refs/jailbee/host/feat/my-current",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--current"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source") == "feat/my-current"


def test_cli_push_current_and_from_are_mutex(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--from", "main", "--current"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_cli_push_current_detached_head_errors(mocker):
    """When --current is set but HEAD is detached, exit 2 with a clear msg."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.get_current_branch", return_value=None)

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--current"])

    assert result.exit_code == 2
    assert "detached head" in result.output.lower()


def test_cli_push_plain_calls_push_to_container(mocker):
    """`--plain` runs the transport-only path (same as no action flag)."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/heads/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="oid",
        ),
    )
    mock_merge = mocker.patch("jailbee.sync.push_and_merge")
    mock_rebase = mocker.patch("jailbee.sync.push_and_rebase")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--plain"])

    assert result.exit_code == 0, result.output
    mock_push.assert_called_once()
    mock_merge.assert_not_called()
    mock_rebase.assert_not_called()


def test_cli_push_plain_merge_rebase_three_way_mutex(mocker):
    """Any two of --merge / --rebase / --plain together → exit 2."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    runner = CliRunner()
    for combo in [
        ["--merge", "--plain"],
        ["--rebase", "--plain"],
        ["--merge", "--rebase"],
    ]:
        result = runner.invoke(app, ["git", "push", "feat-x", *combo])
        assert result.exit_code == 2, f"combo {combo}: {result.output}"
        assert "mutually exclusive" in result.output.lower()


def _push_cfg_factory(action="ask", source="ask", push_from=None):
    """Build a Config mock where the cfg.push policy keys are settable."""
    from jailbee.config import Config

    push: dict[str, str] = {"default_action": action, "default_source": source}
    if push_from is not None:
        push["push_from"] = push_from
    cfg = Config.model_validate({"push": push})
    # Tests don't load YAML; set computed attrs directly so cli code works.
    from pathlib import Path

    object.__setattr__(cfg, "repo_root", Path("/tmp/fake-repo"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")
    return cfg


def test_cli_push_config_default_action_merge_invokes_push_and_merge(mocker):
    """config push.default_action='merge' + no flags → push_and_merge called."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import MergeInContainerResult, PushResult

    cfg = _push_cfg_factory(action="merge", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid=None,
        new_oid="oid",
    )
    mock_merge = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=push_result,
            container_branch="feat-x",
            fast_forward_only=False,
            head_oid="head",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    mock_merge.assert_called_once()


def test_cli_push_config_default_source_current_resolves(mocker):
    """config push.default_source='current' + no flags → current branch used."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="current")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.get_current_branch", return_value="feat/cur")
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/cur",
            source_ref="refs/heads/feat/cur",
            container_ref="refs/jailbee/host/feat/cur",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source") == "feat/cur"


def test_cli_push_flag_wins_over_config_action(mocker):
    """`--rebase` overrides config action='merge' silently."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult, RebaseInContainerResult

    cfg = _push_cfg_factory(action="merge", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid=None,
        new_oid="oid",
    )
    mock_rebase = mocker.patch(
        "jailbee.sync.push_and_rebase",
        return_value=RebaseInContainerResult(
            push=push_result,
            container_branch="feat-x",
            head_oid="head",
        ),
    )
    mock_merge = mocker.patch("jailbee.sync.push_and_merge")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--rebase"])

    assert result.exit_code == 0, result.output
    mock_rebase.assert_called_once()
    mock_merge.assert_not_called()


def test_cli_push_flag_wins_over_config_source(mocker):
    """`--from develop` overrides config source='current'."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="current")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.get_current_branch", return_value="feat/cur")
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="develop",
            source_ref="refs/heads/develop",
            container_ref="refs/jailbee/host/develop",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--from", "develop"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source") == "develop"


def test_cli_push_config_source_current_detached_head_errors(mocker):
    """config source='current' + detached HEAD → exit 2 with documented msg."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="current")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.get_current_branch", return_value=None)

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 2
    assert "push.default_source='current'" in result.output
    assert "detached head" in result.output.lower()


def test_cli_push_config_ask_no_tty_action_errors(mocker):
    """config action='ask' + no flag + no TTY → exit 1 naming the config key."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    # CliRunner has no TTY by default — _stdin_is_interactive returns False.

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 1
    assert "push.default_action" in result.output
    assert "--merge" in result.output and "--rebase" in result.output and "--plain" in result.output


def test_cli_push_config_ask_no_tty_source_errors(mocker):
    """config source='ask' + no flag + no TTY → exit 1 naming the config key."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="ask")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 1
    assert "push.default_source" in result.output
    assert "--from" in result.output and "--current" in result.output


def test_cli_push_picker_action_invoked_when_tty_and_ask(mocker):
    """With TTY available and config action='ask', the action picker opens."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import MergeInContainerResult, PushResult

    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    pick = mocker.patch("jailbee.cli._pick_push_action", return_value="merge")
    push_result = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid=None,
        new_oid="oid",
    )
    mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=push_result,
            container_branch="feat-x",
            fast_forward_only=True,
            head_oid="head",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    pick.assert_called_once()


def test_cli_push_picker_source_invoked_when_tty_and_ask(mocker):
    """With TTY available and config source='ask', the source picker opens."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="ask")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    pick = mocker.patch("jailbee.cli._pick_push_source", return_value="feat/picked")
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/picked",
            source_ref="refs/heads/feat/picked",
            container_ref="refs/jailbee/host/feat/picked",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    pick.assert_called_once()
    assert mock_push.call_args.kwargs.get("source") == "feat/picked"


def test_cli_push_picker_cancel_aborts(mocker):
    """User cancels (Ctrl+C / ESC) — picker returns None → typer.Abort (exit 1)."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.cli._pick_push_action", return_value=None)

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    # typer.Abort exits with code 1.
    assert result.exit_code == 1


def test_pick_push_source_skips_picker_when_current_equals_default(mocker):
    """When current branch == default branch, picker is skipped entirely."""
    from pathlib import Path

    from jailbee.cli import _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value="main")

    select_mock = mocker.patch("questionary.select")

    result = _pick_push_source(cfg)

    assert result == "main"
    select_mock.assert_not_called()


def test_pick_push_source_skips_picker_when_detached_head(mocker):
    """Detached HEAD has no current branch; default is the only option."""
    from pathlib import Path

    from jailbee.cli import _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value=None)

    select_mock = mocker.patch("questionary.select")

    result = _pick_push_source(cfg)

    assert result == "main"
    select_mock.assert_not_called()


def test_pick_push_source_shows_picker_when_branches_differ(mocker):
    """When current != default, the picker is shown with both options."""
    from pathlib import Path

    from jailbee.cli import _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch(
        "jailbee.git.get_current_branch",
        return_value="feat/x",
    )

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = "feat/x"

    result = _pick_push_source(cfg)

    assert result == "feat/x"
    choices = select_mock.call_args.kwargs["choices"]
    assert len(choices) == 2
    assert "default branch" in choices[0].title
    assert "current host branch" in choices[1].title


def test_cli_push_no_name_with_pushable_opens_picker(mocker):
    """With no name + multiple running clone-mode containers, the multi-picker is called."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.lifecycle import ContainerInfo
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name="fake-feat-a",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="clone",
            ),
            ContainerInfo(
                name="fake-feat-b",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="clone",
            ),
        ],
    )
    pick = mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=["fake-feat-a"],
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-a")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/heads/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0, result.output
    pick.assert_called_once()


def test_cli_push_no_name_filters_out_mount_and_stopped(mocker):
    """The picker only sees Running + non-mount containers."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.lifecycle import ContainerInfo
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name="fake-running",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="clone",
            ),
            ContainerInfo(
                name="fake-running2",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="clone",
            ),
            ContainerInfo(
                name="fake-stopped",
                state="Stopped",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="clone",
            ),
            ContainerInfo(
                name="fake-mount",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="mount",
            ),
        ],
    )
    pick = mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=["fake-running"],
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="running")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/heads/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 0, result.output
    # picker was called with only the running clone-mode containers.
    passed = pick.call_args.args[0]
    assert [c.name for c in passed] == ["fake-running", "fake-running2"]


def test_cli_push_no_name_empty_pushable_errors(mocker):
    """No running clone-mode containers → exit 1 with documented message."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.lifecycle import ContainerInfo

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name="fake-mount",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="mount",
            ),
        ],
    )

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 1
    assert "no pushable containers" in result.output.lower()


def test_cli_push_no_name_no_tty_errors(mocker):
    """No name + no TTY → exit 1 with name-required message."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    # CliRunner has no TTY by default.

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 1
    assert "no container" in result.output.lower() or "name required" in result.output.lower()


def test_cli_push_no_name_picker_cancel_aborts(mocker):
    """Picker cancelled → Abort (exit 1)."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.lifecycle import ContainerInfo

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name="fake-a",
                state="Running",
                network=None,
                ip=None,
                memory_limit=None,
                repo="fake",
                mode="clone",
            ),
        ],
    )
    mocker.patch("jailbee.tui.pick_containers_multi", return_value=None)

    result = CliRunner().invoke(app, ["git", "push"])

    assert result.exit_code == 1


def test_cli_push_help_mentions_current_and_plain():
    """`gie git push --help` advertises both new flags."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["git", "push", "--help"])

    assert result.exit_code == 0
    out = result.output
    assert "--current" in out
    assert "--plain" in out
    assert "default action" in out.lower() or "push.default_action" in out


def test_cli_git_group_help_mentions_picker_flow():
    """`gie git --help` mentions that 'gie git push' is interactive."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["git", "--help"])

    assert result.exit_code == 0
    assert "interactive" in result.output.lower() or "picker" in result.output.lower()


def test_pr_head_for_returns_none_for_non_pr_container(mocker):
    from jailbee.cli import _pr_head_for

    incus = mocker.MagicMock()
    incus.config_get.return_value = None  # no user.jailbee.pr label

    assert _pr_head_for(incus, "full-name") is None


def test_pr_head_for_returns_none_when_label_not_int(mocker):
    """A bare MagicMock config_get (existing test style) must not crash."""
    from jailbee.cli import _pr_head_for

    incus = mocker.MagicMock()  # config_get returns a truthy MagicMock

    assert _pr_head_for(incus, "full-name") is None


def test_pr_head_for_reads_number_and_head_ref(mocker):
    from jailbee.cli import _pr_head_for

    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "1234",
        "user.jailbee.branch": "feat/cool",
    }.get(key)

    assert _pr_head_for(incus, "full-name") == (1234, "feat/cool")


def test_refresh_pr_source_resolves_fetches_and_returns_head_ref(mocker):
    from jailbee.cli import _refresh_pr_source
    from jailbee.pr import FetchResult, PrInfo

    cfg = _push_cfg_factory()
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "77",
        "user.jailbee.branch": "feat/pr-branch",
    }.get(key)
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    resolve = mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=77,
            head_ref="feat/pr-branch",
            head_sha="newsha",
            state="OPEN",
            base_ref="main",
        ),
    )
    fetch = mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=True, prev_sha="oldsha", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
        ),
    )

    result = _refresh_pr_source(cfg, incus, "full-name")

    assert result == ("feat/pr-branch", "refs/jailbee/pr/1234/head")
    resolve.assert_called_once_with(cfg.repo_root, 77, remote="origin")
    fetch.assert_called_once()


def test_refresh_pr_source_already_up_to_date_message(mocker, capsys):
    from jailbee.cli import _refresh_pr_source
    from jailbee.pr import FetchResult, PrInfo

    cfg = _push_cfg_factory()
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "3",
        "user.jailbee.branch": "feat/uptodate",
    }.get(key)
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=3,
            head_ref="feat/uptodate",
            head_sha="s",
            state="OPEN",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=False, prev_sha="s", new_sha="s", ref="refs/jailbee/pr/1234/head"
        ),
    )

    result = _refresh_pr_source(cfg, incus, "full-name")

    assert result == ("feat/uptodate", "refs/jailbee/pr/1234/head")
    assert "already up to date" in capsys.readouterr().out


def test_refresh_pr_source_errors_on_non_pr_container(mocker):
    import typer

    from jailbee.cli import _refresh_pr_source

    cfg = _push_cfg_factory()
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    with pytest.raises(typer.Exit) as exc:
        _refresh_pr_source(cfg, incus, "full-name")
    assert exc.value.exit_code == 2


def test_refresh_pr_source_warns_when_pr_not_open(mocker, capsys):
    from jailbee.cli import _refresh_pr_source
    from jailbee.pr import FetchResult, PrInfo

    cfg = _push_cfg_factory()
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "9",
        "user.jailbee.branch": "feat/old",
    }.get(key)
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=9,
            head_ref="feat/old",
            head_sha="s",
            state="MERGED",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=False, prev_sha="s", new_sha="s", ref="refs/jailbee/pr/1234/head"
        ),
    )

    result = _refresh_pr_source(cfg, incus, "full-name")

    assert result == ("feat/old", "refs/jailbee/pr/1234/head")
    assert "MERGED" in capsys.readouterr().out


def test_refresh_pr_source_exits_on_pr_error(mocker):
    import typer

    from jailbee.cli import _refresh_pr_source

    cfg = _push_cfg_factory()
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": "5",
        "user.jailbee.branch": "feat/x",
    }.get(key)
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    from jailbee.pr import PrResolveError

    mocker.patch(
        "jailbee.pr.resolve_pr",
        side_effect=PrResolveError("PR #5 not found"),
    )

    with pytest.raises(typer.Exit) as exc:
        _refresh_pr_source(cfg, incus, "full-name")
    assert exc.value.exit_code == 2


def test_pick_push_source_prepends_pr_head_first(mocker):
    from pathlib import Path

    from jailbee.cli import _PR_HEAD, _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value="main")

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = _PR_HEAD

    result = _pick_push_source(cfg, pr_head=(42, "feat/pr"))

    assert result is _PR_HEAD
    choices = select_mock.call_args.kwargs["choices"]
    # PR head is the first choice even though current == default.
    assert choices[0].value is _PR_HEAD
    assert "PR #42" in choices[0].title


def test_pick_push_source_pr_head_with_distinct_current(mocker):
    from pathlib import Path

    from jailbee.cli import _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value="feat/host")

    select_mock = mocker.patch("questionary.select")
    select_mock.return_value.ask.return_value = "main"

    result = _pick_push_source(cfg, pr_head=(42, "feat/pr"))

    assert result == "main"
    titles = [c.title for c in select_mock.call_args.kwargs["choices"]]
    # PR head first, then default, then current host branch.
    assert "PR #42" in titles[0]
    assert "default branch" in titles[1]
    assert "current host branch" in titles[2]


def test_pick_push_source_offers_base_first(mocker):
    """When base branch is passed and differs from default/current, it is offered first."""
    from pathlib import Path

    from jailbee.cli import _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value="main")

    captured: dict[str, object] = {}

    def _fake_select(msg: str, choices: object, **kw: object) -> object:
        captured["choices"] = choices
        m = mocker.MagicMock()
        assert isinstance(choices, list)
        m.ask.return_value = choices[0].value
        return m

    mocker.patch("questionary.select", side_effect=_fake_select)

    result = _pick_push_source(cfg, base="feat/x")

    assert result == "feat/x"
    choices = captured["choices"]
    assert isinstance(choices, list)
    assert "base branch" in choices[0].title
    assert choices[0].value == "feat/x"


def test_pick_push_source_base_not_duplicated_when_equals_default(mocker):
    """When base branch equals the default branch, it is NOT added as a separate entry."""
    from pathlib import Path

    from jailbee.cli import _pick_push_source
    from jailbee.config import Config

    cfg = Config()
    object.__setattr__(cfg, "repo_root", Path("/tmp/fake"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "fake")

    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value="feat/current")

    captured: dict[str, object] = {}

    def _fake_select(msg: str, choices: object, **kw: object) -> object:
        captured["choices"] = choices
        m = mocker.MagicMock()
        assert isinstance(choices, list)
        m.ask.return_value = choices[0].value
        return m

    mocker.patch("questionary.select", side_effect=_fake_select)

    # base == default branch "main" → no extra entry
    _pick_push_source(cfg, base="main")

    choices = captured["choices"]
    assert isinstance(choices, list)
    titles = [c.title for c in choices]
    # Only default + current, no extra "base branch" entry
    assert not any("base branch" in t for t in titles)
    assert len(choices) == 2  # default + current


def _pr_incus_mock(mocker, *, pr="1234", branch="feat/pr-branch"):
    """An incus mock whose config_get returns PR labels."""
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.pr": pr,
        "user.jailbee.branch": branch,
    }.get(key)
    return incus


def test_cli_push_pr_flag_refreshes_and_pushes_head(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.pr import FetchResult, PrInfo
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="ask")
    incus = _pr_incus_mock(mocker)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus, "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=1234,
            head_ref="feat/pr-branch",
            head_sha="newsha",
            state="OPEN",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=True, prev_sha="old", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
        ),
    )
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/pr-branch",
            source_ref="refs/heads/feat/pr-branch",
            container_ref="refs/jailbee/host/feat/pr-branch",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source") == "feat/pr-branch"


def test_cli_push_pr_flag_on_non_pr_container_errors(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="ask")
    incus = mocker.MagicMock()
    incus.config_get.return_value = None  # no PR label
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus, "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr"])

    assert result.exit_code == 2
    assert "not created from a PR" in result.output


def test_cli_push_pr_pushes_the_gie_pr_ref(mocker):
    """--pr must push exactly the ref `pr.py` fetched the head into.

    `pr.fetch_pr_head` writes `pull/N/head` into `refs/jailbee/pr/<N>/head` —
    never a branch, since git refuses to fetch into a checked-out one. Both
    branch-name candidates are therefore wrong: `refs/heads/<head_ref>` is
    whatever the host happens to have, and `refs/remotes/origin/<head_ref>`
    is stale or absent. The explicit ref also skips the host fetch.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.pr import FetchResult, PrInfo
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="ask")
    incus = _pr_incus_mock(mocker)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "full-name"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=1234,
            head_ref="feat/pr-branch",
            head_sha="newsha",
            state="OPEN",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=True, prev_sha="old", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
        ),
    )
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/pr-branch",
            source_ref="refs/heads/feat/pr-branch",
            container_ref="refs/jailbee/host/feat/pr-branch",
            old_oid=None,
            new_oid="oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source_ref") == "refs/jailbee/pr/1234/head"


def test_cli_push_pr_merge_forwards_the_gie_pr_ref(mocker):
    """The same explicit ref reaches the merge path, not just plain transport."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.pr import FetchResult, PrInfo
    from jailbee.sync import MergeInContainerResult, PushResult

    cfg = _push_cfg_factory(action="ask", source="ask")
    incus = _pr_incus_mock(mocker)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "full-name"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=1234,
            head_ref="feat/pr-branch",
            head_sha="newsha",
            state="OPEN",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=True, prev_sha="old", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
        ),
    )
    push_result = PushResult(
        source="feat/pr-branch",
        source_ref="refs/jailbee/pr/1234/head",
        container_ref="refs/jailbee/host/feat/pr-branch",
        old_oid=None,
        new_oid="oid",
    )
    mock_merge = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=push_result,
            container_branch="feat/pr-branch",
            fast_forward_only=True,
            head_oid="head-oid",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr", "--merge"])

    assert result.exit_code == 0, result.output
    assert mock_merge.call_args.kwargs.get("source_ref") == "refs/jailbee/pr/1234/head"


def test_cli_push_pr_and_from_origin_are_mutex(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr", "--from-origin"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_cli_push_from_local_and_from_origin_are_mutex(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--from-local", "--from-origin"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def _stub_push_cli(mocker, cfg):
    """Mock the CLI surface around a plain `gie git push`, returning the push mock."""
    from jailbee.sync import PushResult

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.git.get_current_branch", return_value="feat/host-side")
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    return mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/remotes/origin/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="newoid",
        ),
    )


def test_cli_push_defaults_leave_ref_pref_to_config(mocker):
    """No ref flag → sync resolves from cfg.push.push_from (prefer_ref=None)."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mock_push = _stub_push_cli(mocker, cfg)

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("prefer_ref") is None


def test_cli_push_from_local_flag_forces_local(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mock_push = _stub_push_cli(mocker, cfg)

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--from-local"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("prefer_ref") == "local"


def test_cli_push_from_origin_flag_forces_origin(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    # Config says local; the flag must win.
    cfg = _push_cfg_factory(action="plain", source="default-branch", push_from="local")
    mock_push = _stub_push_cli(mocker, cfg)

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--from-origin"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("prefer_ref") == "origin"


def test_cli_push_current_implies_local(mocker):
    """--current means "what I have checked out"; origin/<branch> would be older."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mock_push = _stub_push_cli(mocker, cfg)

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--current"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("source") == "feat/host-side"
    assert mock_push.call_args.kwargs.get("prefer_ref") == "local"


def test_cli_push_current_with_from_origin_keeps_origin(mocker):
    """An explicit --from-origin overrides the --current implication."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mock_push = _stub_push_cli(mocker, cfg)

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--current", "--from-origin"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("prefer_ref") == "origin"


def test_cli_push_no_fetch_flag_disables_autofetch(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    mock_push = _stub_push_cli(mocker, cfg)

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--no-fetch"])

    assert result.exit_code == 0, result.output
    assert mock_push.call_args.kwargs.get("fetch") is False


def test_cli_push_warns_about_local_only_commits(mocker):
    """Pushing origin/<source> while local has extra commits must be visible."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/remotes/origin/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="newoid",
            fetched=True,
            local_only_commits=2,
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "2 commit" in result.output
    assert "--from-local" in result.output


def test_cli_push_reports_failed_autofetch(mocker):
    """A failed fetch matters when the (possibly stale) origin ref is what travelled."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="main",
            source_ref="refs/remotes/origin/main",
            container_ref="refs/jailbee/host/main",
            old_oid=None,
            new_oid="newoid",
            fetched=False,
            fetch_error="Could not resolve host github.com",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "Could not resolve host github.com" in result.output


def test_cli_push_silent_when_fetch_failure_did_not_matter(mocker):
    """A branch that simply isn't on origin is the normal stacked-PR case.

    The fetch fails ("couldn't find remote ref"), resolution falls back to
    refs/heads/<source>, and what travelled is not stale — so warning about
    the failed fetch would be pure noise.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/stack-a",
            source_ref="refs/heads/feat/stack-a",
            container_ref="refs/jailbee/host/feat/stack-a",
            old_oid=None,
            new_oid="newoid",
            fetched=False,
            fetch_error="couldn't find remote ref feat/stack-a",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "couldn't find remote ref" not in result.output


def _push_result_with_local_branch(status, *, old_oid="old1234567", branch="main"):
    """A plain PushResult carrying one `ff_container_branch` outcome."""
    from jailbee.sync import LocalBranchUpdate, PushResult

    return PushResult(
        source=branch,
        source_ref=f"refs/heads/{branch}",
        container_ref=f"refs/jailbee/host/{branch}",
        old_oid=None,
        new_oid="new7654321",
        local_branch=LocalBranchUpdate(
            branch=branch,
            status=status,
            old_oid=old_oid,
            new_oid="new7654321",
        ),
    )


def test_cli_push_reports_a_fast_forwarded_container_branch(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=_push_result_with_local_branch("fast-forwarded"),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "old1234" in result.output
    assert "new7654" in result.output


def test_cli_push_reports_a_created_container_branch(mocker):
    """A clone of a host whose HEAD was `main` has no local `dev` until now."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=_push_result_with_local_branch("created", old_oid=None, branch="dev"),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "dev" in result.output
    assert "new7654" in result.output


def test_cli_push_warns_about_a_diverged_container_branch(mocker):
    """Container-only commits on the pushed branch are kept — and said out loud."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=_push_result_with_local_branch("diverged"),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "diverged" in result.output
    assert "old1234" in result.output


def test_cli_push_warns_when_the_container_branch_update_failed(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=_push_result_with_local_branch("failed"),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "stale" in result.output


@pytest.mark.parametrize("status", ["up-to-date", "checked-out"])
def test_cli_push_stays_silent_about_a_benign_container_branch(status, mocker):
    """Nothing to do is nothing to say — these two are the common case."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="plain", source="default-branch")
    _stub_push_cli(mocker, cfg)
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=_push_result_with_local_branch(status),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x"])

    assert result.exit_code == 0, result.output
    assert "local 'main'" not in result.output


def test_cli_push_pr_and_from_are_mutex(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr", "--from", "main"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_cli_push_pr_and_current_are_mutex(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr", "--current"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_cli_push_pr_without_name_errors(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit", return_value=mocker.MagicMock())

    result = CliRunner().invoke(app, ["git", "push", "--pr"])

    assert result.exit_code == 1
    assert "explicit container name" in result.output


def test_cli_push_pr_flag_with_merge_passes_head_to_push_and_merge(mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.pr import FetchResult, PrInfo
    from jailbee.sync import MergeInContainerResult, PushResult

    cfg = _push_cfg_factory(action="ask", source="ask")
    incus = _pr_incus_mock(mocker)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus, "full-name"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=1234,
            head_ref="feat/pr-branch",
            head_sha="newsha",
            state="OPEN",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=True, prev_sha="old", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
        ),
    )
    push_result = PushResult(
        source="feat/pr-branch",
        source_ref="refs/heads/feat/pr-branch",
        container_ref="refs/jailbee/host/feat/pr-branch",
        old_oid=None,
        new_oid="oid",
    )
    mock_merge = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=push_result,
            container_branch="feat/pr-branch",
            fast_forward_only=True,
            head_oid="chead",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--pr", "--merge"])

    assert result.exit_code == 0, result.output
    assert mock_merge.call_args.kwargs.get("source") == "feat/pr-branch"


def test_cli_push_noarg_single_pr_container_offers_pr_head(mocker):
    """No-arg push, exactly one PR container selected -> picker gets pr_head."""
    from typer.testing import CliRunner

    from jailbee.cli import _PR_HEAD, app
    from jailbee.pr import FetchResult, PrInfo
    from jailbee.sync import PushResult

    cfg = _push_cfg_factory(action="plain", source="ask")
    incus = _pr_incus_mock(mocker, pr="55", branch="feat/pr-x")

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    # One pushable PR container.
    container = mocker.MagicMock()
    container.name = "full-name"
    container.state = "Running"
    container.mode = "clone"
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[container],
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    # The PR-head-aware picker returns the sentinel -> triggers refresh.
    pick = mocker.patch(
        "jailbee.cli._pick_push_source",
        return_value=_PR_HEAD,
    )
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=55,
            head_ref="feat/pr-x",
            head_sha="s",
            state="OPEN",
            base_ref="main",
        ),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=FetchResult(
            updated=False, prev_sha="s", new_sha="s", ref="refs/jailbee/pr/1234/head"
        ),
    )
    mock_push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=PushResult(
            source="feat/pr-x",
            source_ref="refs/heads/feat/pr-x",
            container_ref="refs/jailbee/host/feat/pr-x",
            old_oid=None,
            new_oid="o",
        ),
    )

    # --no-confirm: this test is about the PR-head picker offer, not the
    # auto-select confirmation prompt added on top of it (see
    # test_cli_push_multi.py's confirmation tests for that).
    result = CliRunner().invoke(app, ["git", "push", "--no-confirm"])

    assert result.exit_code == 0, result.output
    # picker was given the PR head tuple
    assert pick.call_args.kwargs.get("pr_head") == (55, "feat/pr-x")
    assert mock_push.call_args.kwargs.get("source") == "feat/pr-x"


def test_ls_shows_job_column_when_job_present(make_cfg, tmp_path, monkeypatch, mocker):
    """The JOB column appears only when a container carries a job phase."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Widen the virtual terminal so Rich doesn't elide the JOB cell content.
    monkeypatch.setenv("COLUMNS", "200")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    # PID 4242 is synthetic; force the liveness probe to report alive so the
    # JOB column renders the bare phase rather than "cloning (worker gone)".
    mocker.patch("jailbee.background.worker_alive", return_value=True)

    from jailbee.lifecycle import ContainerInfo

    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name=f"{cfg.container_prefix}-feat-foo",
                state="—",
                network=None,
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
                job_phase="cloning",
                job_pid=4242,
            )
        ],
    )

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "JOB" in result.stdout
    assert "cloning" in result.stdout


def test_ls_job_column_hidden_without_jobs(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())

    from jailbee.lifecycle import ContainerInfo

    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[
            ContainerInfo(
                name=f"{cfg.container_prefix}-feat-foo",
                state="Running",
                network="strict",
                ip="10.0.0.2",
                memory_limit="4GB",
                repo=cfg.container_prefix,
            )
        ],
    )

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "JOB" not in result.stdout


def test_finalize_new_skips_gui_without_session(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    # Default config has jetbrains/chrome disabled, so the GUI block would be
    # skipped entirely. Enable JetBrains autostart so launch_ide is truthy and
    # the "no graphical session -> warn" path is actually exercised.
    cfg = cfg.model_copy(
        update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": True, "autostart": True})}
    )
    incus = mocker.MagicMock()
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=False)
    warn_mock = mocker.patch("jailbee.autostart.maybe_warn_no_gui")

    from jailbee.cli import _finalize_new

    _finalize_new(cfg, incus, f"{cfg.container_prefix}-feat-foo", launch_gui=True)

    # No graphical session -> GUI launch is skipped with a warning. (The PR
    # label is persisted in new_container, not here — see test_lifecycle.py.)
    warn_mock.assert_called_once()


def test_new_worker_success_deletes_op_and_finalizes(make_cfg, tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    nc_mock = mocker.patch(
        "jailbee.lifecycle.new_container",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    fin_mock = mocker.patch("jailbee.cli._finalize_new")

    import json
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import NewContainerOptions

    name = f"{cfg.container_prefix}-feat-foo"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
        )

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
    )
    job = background.op_to_job(opts, container_name=name, log_path="/l")
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job_file)])
    assert result.exit_code == 0
    nc_mock.assert_called_once()
    fin_mock.assert_called_once()
    with Session(get_engine()) as s:
        assert background.list_jobs(s, cfg.container_prefix) == {}


def test_new_worker_failure_marks_op_failed(make_cfg, tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.lifecycle.new_container",
        side_effect=RuntimeError("clone exploded"),
    )

    import json
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import NewContainerOptions

    name = f"{cfg.container_prefix}-feat-foo"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
        )

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
    )
    job = background.op_to_job(opts, container_name=name, log_path="/l")
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job_file)])
    assert result.exit_code == 1
    with Session(get_engine()) as s:
        ops = background.list_jobs(s, cfg.container_prefix)
    assert ops[name].phase == background.PHASE_FAILED
    assert "clone exploded" in (ops[name].error_msg or "")


def _drop_job_table() -> None:
    """Delete the `background_op` table under a live process, the way a
    concurrently running older jailbee used to (`drop_all` + `create_all` on
    meeting a newer database). The engine is already bootstrapped and cached
    by then, so nothing recreates the table for the rest of the process."""
    from jailbee.db import get_engine

    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE background_op")


def test_new_worker_survives_a_reset_job_table(make_cfg, tmp_path, monkeypatch, mocker):
    """A vanished job table costs the phase updates, not the container."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    name = f"{cfg.container_prefix}-feat-foo"

    def _create(_cfg, _incus, _opts, *, on_phase=None, confirm_fn=None):
        assert on_phase is not None
        on_phase("creating")
        on_phase("cloning")
        return name

    nc_mock = mocker.patch("jailbee.lifecycle.new_container", side_effect=_create)
    fin_mock = mocker.patch("jailbee.cli._finalize_new")

    import json
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import NewContainerOptions

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
        )
    _drop_job_table()

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
    )
    job = background.op_to_job(opts, container_name=name, log_path="/l")
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job_file)])

    # The container was created and finalised; only the bookkeeping was lost,
    # and it says so once per failed write instead of raising.
    assert result.exit_code == 0, result.output
    nc_mock.assert_called_once()
    fin_mock.assert_called_once()
    combined = result.output + (result.stderr or "")
    assert "could not record phase 'creating'" in combined
    assert "no such table" in combined


def test_new_worker_reports_its_own_failure_when_the_job_table_is_gone(
    make_cfg, tmp_path, monkeypatch, mocker
):
    """A real failure still exits 1 — not replaced by a bookkeeping traceback."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.lifecycle.new_container",
        side_effect=RuntimeError("clone exploded"),
    )

    import json

    from jailbee import background
    from jailbee.lifecycle import NewContainerOptions

    name = f"{cfg.container_prefix}-feat-foo"
    _drop_job_table()

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
    )
    job = background.op_to_job(opts, container_name=name, log_path="/l")
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job_file)])

    assert result.exit_code == 1
    # typer.Exit, i.e. the worker's own controlled exit — not the OperationalError
    # `fail_job` would otherwise have raised out of the except block.
    assert isinstance(result.exception, SystemExit)
    combined = result.output + (result.stderr or "")
    assert "clone exploded" in combined
    assert "could not mark the job failed" in combined


def test_new_background_survives_a_missing_job_table(make_cfg, tmp_path, monkeypatch, mocker):
    """The foreground has already spawned the worker: a failed insert warns."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.MagicMock(docker_registry_mirror=mocker.MagicMock(enabled=False)),
    )
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.egress_pool.register_repo")
    refresh = mocker.MagicMock()
    refresh.status = "ok"
    mocker.patch("jailbee.egress_pool.refresh_pool", return_value=refresh)
    _stub_preflight(mocker, cfg)

    popen_mock = mocker.patch("jailbee.cli.subprocess.Popen")
    fake_proc = mocker.MagicMock()
    fake_proc.pid = 9999
    popen_mock.return_value = fake_proc

    from typer.testing import CliRunner

    from jailbee.cli import app

    # The foreground's own egress refresh is stubbed, so the engine is first
    # created (and bootstrapped) by the job insert — drop the table after a
    # deliberate warm-up so the cached engine cannot recreate it.
    from jailbee.db import get_engine

    get_engine()
    _drop_job_table()

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 0, result.output
    popen_mock.assert_called_once()
    combined = result.output + (result.stderr or "")
    assert "could not record the new job row" in combined
    assert "is being created in the background" in combined


_PREFLIGHT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _stub_preflight(mocker, cfg, *, branch_autostart=None, deviation=None):
    """Let `_preflight_background_new` resolve a clone ref without a real repo.

    The branch resolves through origin mode to `_PREFLIGHT_SHA` (fetch and
    rev-parse stubbed). `branch_autostart` is what the target branch commits;
    None means it commits no `.jailbee/config.yaml`. The privilege baseline is left
    unreadable on purpose, so the gate falls back to comparing against the
    checkout — the shape that makes a widening visible in these tests.
    """
    from jailbee.branch_config import AutostartDeviation, BranchAutostart

    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=_PREFLIGHT_SHA)
    mocker.patch("jailbee.git.show_file_at_ref", return_value=None)
    loaded = None
    if branch_autostart is not None:
        loaded = BranchAutostart(
            cfg=cfg.model_copy(update={"autostart": branch_autostart}),
            deviation=deviation or AutostartDeviation(added=("on_create[seed]",)),
            source=f"{_PREFLIGHT_SHA[:12]} (main)",
        )
    return mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=loaded)


def _mount_step_autostart(mount="aws"):
    """An autostart whose step attaches a host mount — always gated."""
    from jailbee.config import Autostart, AutostartStep

    return Autostart(on_create=[AutostartStep(name="seed", run="./s", mounts=[mount])])


def _background_new_env(make_cfg, tmp_path, monkeypatch, mocker):
    """The `gie new --background` fixture set: config, Incus, egress, Popen."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.MagicMock(docker_registry_mirror=mocker.MagicMock(enabled=False)),
    )
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=False)
    # The escalation gate probes the baseline ref before deciding whether to
    # prompt. `subprocess.Popen` is mocked below to prove no background worker
    # was spawned, and that patch is process-wide — an unstubbed git call here
    # would be counted as a spawn. See `_baseline_autostart`.
    mocker.patch("jailbee.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.egress_pool.register_repo")
    refresh = mocker.MagicMock()
    refresh.status = "ok"
    mocker.patch("jailbee.egress_pool.refresh_pool", return_value=refresh)

    popen_mock = mocker.patch("jailbee.cli.subprocess.Popen")
    fake_proc = mocker.MagicMock()
    fake_proc.pid = 9999
    popen_mock.return_value = fake_proc
    return cfg, popen_mock


def _spawned_job(popen_mock):
    """The job dict the spawned worker will read."""
    import json
    from pathlib import Path

    argv = popen_mock.call_args.args[0]
    return json.loads(Path(argv[argv.index("--job") + 1]).read_text())


def test_new_background_asks_about_an_escalation_before_detaching(
    make_cfg, tmp_path, monkeypatch, mocker
):
    """The regression's fix: the question is asked while a terminal still exists.

    The worker inherits the answer, so it never has to decline one it cannot ask.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg, popen_mock = _background_new_env(make_cfg, tmp_path, monkeypatch, mocker)
    _stub_preflight(mocker, cfg, branch_autostart=_mount_step_autostart())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("jailbee.tui.default_confirm", return_value=True)

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 0, result.stdout
    confirm.assert_called_once()
    popen_mock.assert_called_once()
    # The accepted ref travels to the worker, pinned to the commit that was shown.
    assert _spawned_job(popen_mock)["opts"]["approved_autostart_ref"] == _PREFLIGHT_SHA


def test_new_background_declining_creates_nothing(make_cfg, tmp_path, monkeypatch, mocker):
    """No worker, no job row, no log — declining costs the user nothing."""
    from sqlmodel import Session
    from typer.testing import CliRunner

    from jailbee import background
    from jailbee.cli import app
    from jailbee.db import get_engine

    cfg, popen_mock = _background_new_env(make_cfg, tmp_path, monkeypatch, mocker)
    _stub_preflight(mocker, cfg, branch_autostart=_mount_step_autostart())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.tui.default_confirm", return_value=False)

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 2
    popen_mock.assert_not_called()
    with Session(get_engine()) as s:
        assert background.list_jobs(s, cfg.container_prefix) == {}


def test_new_background_without_a_terminal_says_how_to_accept(
    make_cfg, tmp_path, monkeypatch, mocker
):
    """`default_confirm` reads a closed stdin as "no"; that must not read as a
    deliberate refusal."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg, popen_mock = _background_new_env(make_cfg, tmp_path, monkeypatch, mocker)
    _stub_preflight(mocker, cfg, branch_autostart=_mount_step_autostart())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 2
    assert "--yes" in result.stdout + (result.stderr or "")
    popen_mock.assert_not_called()


def test_new_background_does_not_ask_about_a_trusted_network_widening(
    make_cfg, tmp_path, monkeypatch, mocker
):
    """A `loose` step from the operator's own repo detaches without a question."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.config import Autostart, AutostartStep

    cfg, popen_mock = _background_new_env(make_cfg, tmp_path, monkeypatch, mocker)
    _stub_preflight(
        mocker,
        cfg,
        branch_autostart=Autostart(
            on_start=[AutostartStep(name="warmup", run="./w", network="loose")]
        ),
    )
    confirm = mocker.patch("jailbee.tui.default_confirm", return_value=False)

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 0, result.stdout
    confirm.assert_not_called()
    popen_mock.assert_called_once()
    assert _spawned_job(popen_mock)["opts"]["approved_autostart_ref"] is None


def test_new_background_job_records_that_the_fetch_already_happened(
    make_cfg, tmp_path, monkeypatch, mocker
):
    """The worker must not fetch again: a second fetch is another round trip —
    and another hardware-key touch — and could resolve a newer commit than the
    one the pre-flight assessed."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg, popen_mock = _background_new_env(make_cfg, tmp_path, monkeypatch, mocker)
    fetch = _stub_preflight(mocker, cfg)  # no branch config: nothing to assess

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 0, result.stdout
    fetch.assert_called_once()  # the branch config *was* read in the foreground
    assert _spawned_job(popen_mock)["opts"]["autofetch_done"] is True


def test_new_background_reports_an_unresolvable_ref_in_the_terminal(
    make_cfg, tmp_path, monkeypatch, mocker
):
    """Resolution failures used to surface only in the worker's log."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _cfg, popen_mock = _background_new_env(make_cfg, tmp_path, monkeypatch, mocker)
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=False)
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=None)

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 2
    assert "not found" in result.stdout + (result.stderr or "")
    popen_mock.assert_not_called()


def test_new_background_spawns_worker_and_returns(make_cfg, tmp_path, monkeypatch, mocker):
    """--background spawns a detached worker, records the op, returns immediately."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.MagicMock(docker_registry_mirror=mocker.MagicMock(enabled=False)),
    )
    # Branch does not already exist -> no confirm prompt.
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=False)
    # Stub the egress pool register/refresh the foreground performs.
    mocker.patch("jailbee.egress_pool.register_repo")
    refresh = mocker.MagicMock()
    refresh.status = "ok"
    mocker.patch("jailbee.egress_pool.refresh_pool", return_value=refresh)
    # …and the host-side ref resolution the foreground pre-flight performs.
    _stub_preflight(mocker, cfg)

    popen_mock = mocker.patch("jailbee.cli.subprocess.Popen")
    fake_proc = mocker.MagicMock()
    fake_proc.pid = 9999
    popen_mock.return_value = fake_proc

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])
    assert result.exit_code == 0, result.stdout
    popen_mock.assert_called_once()
    # Worker invoked via `python -m jailbee _new-worker`.
    argv = popen_mock.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_new-worker"]

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        ops = background.list_jobs(s, cfg.container_prefix)
    assert len(ops) == 1
    row = next(iter(ops.values()))
    assert row.pid == 9999
    assert row.phase == background.PHASE_STARTING


def test_new_background_rejects_attach(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background", "--attach", "shell"])
    assert result.exit_code == 2
    # error() writes to stderr; Click 8.3 keeps streams separate, so read the
    # combined output (consistent with the other error-path tests in this file).
    assert "background" in result.output.lower()


def test_new_background_silently_drops_config_after_new(make_cfg, tmp_path, monkeypatch, mocker):
    """`after_new: tmux` in config yields to --background; only CLI flags conflict."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    cfg = make_cfg(repo).model_copy(update={"after_new": "tmux"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.MagicMock(docker_registry_mirror=mocker.MagicMock(enabled=False)),
    )
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.egress_pool.register_repo")
    refresh = mocker.MagicMock()
    refresh.status = "ok"
    mocker.patch("jailbee.egress_pool.refresh_pool", return_value=refresh)
    _stub_preflight(mocker, cfg)
    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=9999)
    attach_tmux = mocker.patch("jailbee.cli._attach_tmux")

    from typer.testing import CliRunner

    from jailbee.cli import app

    result = CliRunner().invoke(app, ["new", "feat/foo", "--background"])

    assert result.exit_code == 0, result.stdout
    popen.assert_called_once()
    attach_tmux.assert_not_called()


# ---------------------------------------------------------------------------
# push: default_source='base' — sentinel resolved per container
# ---------------------------------------------------------------------------


def test_push_base_source_resolves_per_container(mocker):
    """cfg.push.default_source='base' → source resolved from container's
    user.jailbee.base_branch label inside _do_single_push; push_and_merge
    is called with that branch as the source."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.sync import MergeInContainerResult, PushResult

    # default_source='base' is the real config default; be explicit.
    cfg = _push_cfg_factory(action="merge", source="base")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)

    incus_mock = mocker.MagicMock()
    # resolve_container_name (called inside _do_single_push) needs exists().
    incus_mock.exists.return_value = True
    # config_get must return the base branch label.
    incus_mock.config_get.side_effect = lambda full, key: (
        "dev" if key == "user.jailbee.base_branch" else None
    )
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus_mock, "fake-feat-x"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    push_result = PushResult(
        source="dev",
        source_ref="refs/heads/dev",
        container_ref="refs/jailbee/host/dev",
        old_oid=None,
        new_oid="abc1234",
    )
    mock_merge = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=push_result,
            container_branch="feat-x",
            fast_forward_only=False,
            head_oid="head1234",
        ),
    )

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--merge"])

    assert result.exit_code == 0, result.output
    mock_merge.assert_called_once()
    _, kwargs = mock_merge.call_args
    assert kwargs["source"] == "dev"


def test_push_base_source_errors_when_no_base_label(mocker):
    """cfg.push.default_source='base' and no user.jailbee.base_branch label
    → non-zero exit with a message mentioning the base branch."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _push_cfg_factory(action="merge", source="base")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)

    incus_mock = mocker.MagicMock()
    incus_mock.exists.return_value = True
    # No base branch label present.
    incus_mock.config_get.return_value = None
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus_mock, "fake-feat-x"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")

    result = CliRunner().invoke(app, ["git", "push", "feat-x", "--merge"])

    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "base branch" in combined.lower()


def test_ls_job_cell_labels_destroy_starting_as_destroying(make_cfg, tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-dying",
            "status": "Running",
            "profiles": ["default", "myrepo-net-strict"],
            "state": {"network": {}},
            "config": {},
        }
    ]
    incus.config_get.return_value = None
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.background.worker_alive", return_value=True)
    # `gie ls` collects git status via probe_many_parallel; stub it out.
    mocker.patch("jailbee.lifecycle.probe_many_parallel", return_value={})

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-dying",
            container_prefix="myrepo",
            branch=None,
            pid=999,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )

    import json as _json

    from typer.testing import CliRunner

    from jailbee.cli import app

    # Use JSON output so a narrow table width can't truncate the cell.
    result = CliRunner().invoke(app, ["ls", "--format", "json", "--fields", "name,job"])
    assert result.exit_code == 0, result.stdout
    rows = _json.loads(result.stdout)
    dying = next(r for r in rows if r["name"] == "dying")
    assert dying["job"] == "destroying"


def test_print_bridge_direction_format(capsys):
    from jailbee import cli

    cli._print_bridge_direction("feat/foo", "container", "dev", "host")
    out = capsys.readouterr().out
    assert "feat/foo (container) ──▶ dev (host)" in out


def test_do_single_pull_prints_direction(mocker, capsys):
    from jailbee import cli

    cfg = mocker.MagicMock()
    cfg.repo_root = "/repo"
    incus = mocker.MagicMock()

    merge_result = mocker.MagicMock()
    merge_result.branch = "feat/foo"
    merge_result.into_branch = "dev"
    merge_result.head_oid = "abcdef1234567"
    merge_result.pre_merge_head = None
    merge_result.fetch = mocker.MagicMock()
    mocker.patch("jailbee.sync.merge_from_container", return_value=merge_result)
    mocker.patch("jailbee.cli._print_fetch_summary")
    cleanup = mocker.MagicMock()
    cleanup.skipped_reason = None
    cleanup.destroyed = False
    cleanup.deleted_branch = False
    cleanup.cleanup_error = None
    mocker.patch("jailbee.sync.run_post_merge_cleanup", return_value=cleanup)
    mocker.patch("jailbee.sync.compute_submodule_moves", return_value=[])
    mocker.patch("jailbee.sync.render_submodule_report", return_value=None)

    cli._do_single_pull(
        cfg,
        incus,
        "feat-foo",
        branch=None,
        ff_only=False,
        into="dev",
        allow_checkout=False,
        destroy_policy="never",
        branch_policy="never",
    )
    out = capsys.readouterr().out
    assert "feat/foo (container) ──▶ dev (host)" in out


def _dev_push_result():
    """A real PushResult — `_print_push_summary` reads numeric fields on it."""
    from jailbee.sync import PushResult

    return PushResult(
        source="dev",
        source_ref="refs/heads/dev",
        container_ref="refs/jailbee/host/dev",
        old_oid="1111111",
        new_oid="2222222",
    )


def test_do_single_push_merge_prints_direction(mocker, capsys):
    from jailbee import cli
    from jailbee.sync import MergeInContainerResult

    cfg = mocker.MagicMock()
    incus = mocker.MagicMock()

    mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=_dev_push_result(),
            container_branch="feat/foo",
            fast_forward_only=False,
            head_oid="abcdef1234567",
        ),
    )

    cli._do_single_push(cfg, incus, "feat-foo", source="dev", action="merge")
    out = capsys.readouterr().out
    assert "dev (host) ──▶ feat/foo (container)" in out


def test_do_single_push_rebase_prints_direction(mocker, capsys):
    from jailbee import cli
    from jailbee.sync import RebaseInContainerResult

    cfg = mocker.MagicMock()
    incus = mocker.MagicMock()

    mocker.patch(
        "jailbee.sync.push_and_rebase",
        return_value=RebaseInContainerResult(
            push=_dev_push_result(),
            container_branch="feat/foo",
            head_oid="abcdef1234567",
        ),
    )

    cli._do_single_push(cfg, incus, "feat-foo", source="dev", action="rebase")
    out = capsys.readouterr().out
    assert "dev (host) ──▶ feat/foo (container)" in out


def test_do_single_push_force_prints_direction(mocker, capsys):
    from jailbee import cli
    from jailbee.sync import ResetInContainerResult

    cfg = mocker.MagicMock()
    incus = mocker.MagicMock()

    mocker.patch(
        "jailbee.sync.push_and_reset",
        return_value=ResetInContainerResult(
            push=_dev_push_result(),
            container_branch="feat/foo",
            head_oid="abcdef1234567",
            discarded_commits=0,
            old_branch_oid="3333333",
        ),
    )

    cli._do_single_push(cfg, incus, "feat-foo", source="dev", action="force")
    out = capsys.readouterr().out
    assert "dev (host) ──▶ feat/foo (container)" in out


def test_do_single_push_plain_prints_direction(mocker, capsys):
    from jailbee import cli

    cfg = mocker.MagicMock()
    incus = mocker.MagicMock()

    mocker.patch("jailbee.sync.push_to_container", return_value=_dev_push_result())

    cli._do_single_push(cfg, incus, "feat-foo", source="dev", action="plain")
    out = capsys.readouterr().out
    assert "dev (host) ──▶ refs/jailbee/host/dev (container)" in out


def test_new_cmd_threads_yes_into_opts(tmp_path, mocker):
    """--yes must reach new_container, not just the branch-exists prompt."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _repo, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-autostart", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert new_container.call_args.args[2].assume_yes is True


def test_new_cmd_without_yes_leaves_assume_yes_false(tmp_path, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    _repo, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    assert new_container.call_args.args[2].assume_yes is False


def test_new_cmd_mount_mode_also_carries_assume_yes(tmp_path, mocker):
    """Both opts constructions stay symmetric."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _repo, new_container = _setup_new_cmd_env(tmp_path, mocker)

    result = CliRunner().invoke(app, ["new", "boxname", "--mount", "--no-autostart", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert new_container.call_args.args[2].assume_yes is True


def test_new_worker_injects_a_declining_confirm_fn(tmp_path, mocker):
    """A detached worker must never block on stdin."""
    import json

    from typer.testing import CliRunner

    from jailbee.background import op_to_job
    from jailbee.cli import app
    from jailbee.lifecycle import NewContainerOptions

    _repo, new_container = _setup_new_cmd_env(tmp_path, mocker)
    mocker.patch("jailbee.cli._finalize_new")
    mocker.patch("jailbee.background.set_phase")
    mocker.patch("jailbee.background.delete_job")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=False,
    )
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(op_to_job(opts, container_name="myrepo-feat-x", log_path=str(tmp_path / "l")))
    )

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job_file)])

    assert result.exit_code == 0, result.stdout
    confirm_fn = new_container.call_args.kwargs["confirm_fn"]
    assert confirm_fn("anything?") is False


def test_registry_up_drives_a_live_status_line(mocker, tmp_path):
    """The CLI must feed `registry_up`'s steps into the spinner.

    `registry_up` growing an `on_step` parameter achieves nothing if the one
    caller that has a terminal ignores it — this pins the wiring, not the
    callback's existence.
    """
    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.incus.Incus")

    def fake_up(incus, gcfg, *, recreate=False, on_step):
        on_step("creating the container")
        on_step("installing the registry proxy")

    mocker.patch("jailbee.registry.registry_up", side_effect=fake_up)
    status = mocker.MagicMock()
    mocker.patch("jailbee.tui.console.status", return_value=status)
    status.__enter__.return_value = status

    result = CliRunner().invoke(app, ["registry", "up"])

    assert result.exit_code == 0, result.output
    updates = [call.args[0] for call in status.update.call_args_list]
    assert any("creating the container" in u for u in updates)
    assert any("installing the registry proxy" in u for u in updates)


def test_registry_up_reports_a_provisioning_failure_without_a_traceback(mocker):
    """Typer installs no global handler, so an escaping IncusError reached
    the user as a Rich traceback with the whole provisioning script in it —
    twice, once per exception in the chain."""
    from jailbee.cli import app
    from jailbee.incus import IncusError

    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.registry.registry_up",
        side_effect=IncusError("`incus exec jailbee-registry-mirror ...` timed out after 600s"),
    )

    result = CliRunner().invoke(app, ["registry", "up"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "timed out after 600s" in result.output
    assert "Traceback" not in result.output


def test_registry_up_reports_a_dead_service_without_a_traceback(mocker):
    """`_ensure_service_active` raises RuntimeError with the `--recreate`
    advice already written for a human; it must reach them as advice."""
    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.registry.registry_up",
        side_effect=RuntimeError(
            "jailbee-registry-proxy.service did not become active within 60s. "
            "Run `jailbee registry up --recreate` to rebuild the mirror container."
        ),
    )

    result = CliRunner().invoke(app, ["registry", "up"])

    assert result.exit_code == 1
    assert "--recreate" in result.output
    assert "Traceback" not in result.output


def test_base_build_reports_a_provisioning_failure_without_a_traceback(mocker):
    """apt's own output is the diagnosis; a Rich traceback pushes it off the
    top of the screen."""
    from jailbee.cli import app
    from jailbee.incus import IncusError

    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.golden.build_golden_image",
        side_effect=IncusError("build failed (exit 100): E: Unable to locate package"),
    )

    result = CliRunner().invoke(app, ["base", "build"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    # Rich wraps at the terminal width, so match a fragment that cannot wrap.
    assert "Unable to locate" in result.output
    assert "Traceback" not in result.output


def test_new_cmd_aborts_in_strict_when_the_mirror_is_down(tmp_path, mocker):
    """strict + Docker: the mirror is the container's only route to Docker
    Hub, so creating the container anyway would produce a silently broken
    `docker pull`.

    Note: the mocked message here is the realistic one `compute_mirror_endpoint`
    actually raises (CA already includes its own "registry up" hint, so the
    old, pre-gating code aborted on this exact input too). This test does not
    distinguish old from new gating behaviour — it only guards against a
    future regression that relaxes the strict abort. The gating behaviour
    itself (Docker-stack detection, strict vs. loose) is proven by the loose
    test and the no-docker-stack test below.
    """
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text(
        "golden:\n  stacks:\n    docker: true\ndefaults:\n  network: strict\n"
    )
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path / "registry"),
        ),
    )
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        side_effect=ValueError(
            "jailbee-registry-mirror container not found. Run 'jailbee registry up' first."
        ),
    )
    new_container = mocker.patch("jailbee.lifecycle.new_container")

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 1
    # `tui.error` prints to err_console (stderr) and this typer version's
    # CliRunner keeps the streams apart — hence the repo idiom from
    # tests/test_cli_port.py:134.
    assert "registry up" in result.stdout + (result.stderr or "")
    new_container.assert_not_called()


def test_new_cmd_warns_but_continues_in_loose_when_the_mirror_is_down(tmp_path, mocker):
    """loose pulls go direct, so the mirror is only a cache there."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text(
        "golden:\n  stacks:\n    docker: true\ndefaults:\n  network: loose\n"
    )
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path / "registry"),
        ),
    )
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        side_effect=ValueError("jailbee-registry-mirror container not found."),
    )
    new_container = mocker.patch("jailbee.lifecycle.new_container")
    new_container.return_value = "myrepo-feat-x"

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    opts = new_container.call_args.args[2]
    assert opts.mirror_endpoint is None
    assert opts.mirror_ca_path is None


def test_new_cmd_skips_the_mirror_preflight_without_a_docker_stack(tmp_path, mocker):
    """The whole point: a repo with no Docker never touches the mirror, so a
    missing mirror container is not its problem."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text("{}\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path / "registry"),
        ),
    )
    compute = mocker.patch("jailbee.docker_daemon.compute_mirror_endpoint")
    new_container = mocker.patch("jailbee.lifecycle.new_container")
    new_container.return_value = "myrepo-feat-x"

    result = CliRunner().invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.stdout
    compute.assert_not_called()
    assert new_container.call_args.args[2].mirror_endpoint is None


def test_init_cmd_warns_instead_of_aborting_when_the_mirror_is_down(tmp_path, mocker):
    """`init` is documented to run before `registry up`, and the ACL's mirror
    rule is added by the next `apply` / refresh anyway."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    repo = _setup_repo(tmp_path, "myrepo")
    (repo / ".jailbee" / "config.yaml").write_text("golden:\n  stacks:\n    docker: true\n")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        side_effect=ValueError("jailbee-registry-mirror container not found."),
    )
    run_init = mocker.patch("jailbee.init_command.run_init")
    # Left unmocked, install_systemd_units writes real unit files into
    # ~/.config/systemd/user/ and runs systemctl against the developer's
    # session — see the comment in test_init_resolves_mirror_endpoint_and_calls_run_init.
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("jailbee.egress_pool.register_repo")

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.stdout
    run_init.assert_called_once()
    assert run_init.call_args.kwargs["mirror_endpoint"] is None


# ---------------------------------------------------------------------------
# _load_or_exit: scratch (config-less) directories
# ---------------------------------------------------------------------------


def _scratch_cwd(
    tmp_path,
    monkeypatch,
    mocker,
    *,
    git: bool = False,
    global_yaml: str = "{}\n",
):
    """Shared setup for CLI tests exercising a directory with no
    `.jailbee/config.yaml` — the "scratch" config path.

    Points $XDG_CONFIG_HOME at a fresh `global.yaml` carrying `global_yaml`,
    creates `tmp_path/tutkimus` (with a `.git/` when `git=True`, for commands
    that need a repo to look like one), chdirs into it, and patches
    `detect_default_branch` so config loading never shells out to git.
    Returns the directory. Shared across every scratch-directory CLI test
    (this task and the config show/validate and `jb new` pre-flight tests
    that build on it).
    """
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text(global_yaml)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    repo = tmp_path / "tutkimus"
    if git:
        (repo / ".git").mkdir(parents=True)
    else:
        repo.mkdir(parents=True)
    monkeypatch.chdir(repo)

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")

    return repo


def test_ls_works_in_a_directory_with_no_config(tmp_path, monkeypatch, mocker):
    """The whole point of hooking in at the loader: commands that never
    mention scratch work in a scratch directory, and the container prefix
    they operate on is derived from the directory name — not merely "no
    error was raised"."""
    _scratch_cwd(tmp_path, monkeypatch, mocker, git=True)
    incus_mock = mocker.patch("jailbee.incus.Incus")
    incus_mock.return_value.list_containers.return_value = [
        {
            "name": "tutkimus-feat-x",
            "status": "Running",
            "profiles": ["default", "tutkimus-base", "tutkimus-binds", "tutkimus-net-strict"],
            "state": None,
            "config": {},
        }
    ]
    incus_mock.return_value.config_get.return_value = None

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout
    # Only reachable if `cfg.container_prefix` was synthesized as "tutkimus"
    # (slugified from the directory name) — a config load failure would have
    # exited 1 before any container was ever filtered or printed.
    assert "feat-x" in result.stdout


def test_ls_still_errors_when_scratch_is_disabled(tmp_path, monkeypatch, mocker):
    _scratch_cwd(
        tmp_path,
        monkeypatch,
        mocker,
        git=True,
        global_yaml="scratch:\n  enabled: false\n",
    )

    result = CliRunner().invoke(app, ["ls"])

    assert result.exit_code == 1
    assert "jailbee config init" in result.output
    # Distinguishes the new `ConfigNotFoundError` raised by
    # `load_repo_config`/`_synthesize_repo_config` (which names
    # `scratch.enabled` and the global config file) from the old
    # `find_repo_config` message, which never mentions either — without
    # this, the assertions above pass whether or not `_load_or_exit` was
    # ever changed to call the new loader.
    assert "scratch.enabled" in result.output


# ---------------------------------------------------------------------------
# `config show` / `config validate` in a scratch (config-less) directory
# ---------------------------------------------------------------------------


def test_config_validate_in_a_scratch_directory(tmp_path, monkeypatch, mocker):
    """`config validate` must report the synthesized source, not traceback on
    the uncaught `ConfigNotFoundError` `_resolve_config_path` used to raise
    for a directory with no `.jailbee/config.yaml`."""
    from jailbee.config import SCRATCH_ORIGIN_SUFFIX

    _scratch_cwd(tmp_path, monkeypatch, mocker)
    gpath = tmp_path / ".config" / "jailbee" / "global.yaml"

    result = CliRunner().invoke(app, ["config", "validate"])
    # Rich wraps long lines (mid-word, no hyphen) at the runner's terminal
    # width, so compare against the output with newlines collapsed rather
    # than the raw string.
    collapsed = result.output.replace("\n", "")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    # Named for the source label, not merely "it didn't crash": a stray
    # `path = _resolve_config_path(config)` left in place would traceback
    # before this line is ever printed, and a label that silently fell back
    # to some other string would pass an exit-code-only assertion.
    assert f"Schema OK: {gpath}{SCRATCH_ORIGIN_SUFFIX}" in collapsed


def test_config_validate_reports_a_scratch_config_typo(tmp_path, monkeypatch, mocker):
    """A schema error in `global.yaml`'s `scratch.config` block must surface
    as a normal validation failure, labeled so the user knows it came from
    the synthesized layer rather than a (nonexistent) repo config file."""
    _scratch_cwd(
        tmp_path,
        monkeypatch,
        mocker,
        global_yaml="scratch:\n  config:\n    defaults:\n      cpu: banana\n",
    )

    result = CliRunner().invoke(app, ["config", "validate"])

    assert result.exit_code == 1
    assert "scratch.config" in result.output


def test_config_show_layer_repo_prints_the_synthesized_layer(tmp_path, monkeypatch, mocker):
    """`--layer repo` in a scratch directory prints the in-memory synthesized
    layer (what `scratch_repo_layer` builds), not a nonexistent file path."""
    _scratch_cwd(tmp_path, monkeypatch, mocker)

    result = CliRunner().invoke(app, ["config", "show", "--layer", "repo"])

    assert result.exit_code == 0, result.output
    assert "container_prefix: tutkimus" in result.output
    assert "jailbee-scratch-base" in result.output


def test_config_show_default_layer_in_a_scratch_directory(tmp_path, monkeypatch, mocker):
    """The default (`effective`) layer must also survive a scratch directory.

    Ruling 6: the brief's own replacement snippet for this branch left the
    unconditional `path = _resolve_config_path(config)` call in place, which
    still tracebacks with an uncaught `ConfigNotFoundError` here — exactly
    the directory this feature exists for. Only testing `--layer repo` (as
    the brief does) would leave this regression unverified.
    """
    from jailbee.config import SCRATCH_ORIGIN_SUFFIX

    _scratch_cwd(tmp_path, monkeypatch, mocker)
    gpath = tmp_path / ".config" / "jailbee" / "global.yaml"

    result = CliRunner().invoke(app, ["config", "show"])
    # Rich wraps long lines (mid-word, no hyphen) at the runner's terminal
    # width, so compare against the output with newlines collapsed rather
    # than the raw string.
    collapsed = result.output.replace("\n", "")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert f"merged from global + {gpath}{SCRATCH_ORIGIN_SUFFIX}" in collapsed
    assert "container_prefix: tutkimus" in result.output


# ---------------------------------------------------------------------------
# `jb new` pre-flights in a scratch (config-less) directory
# ---------------------------------------------------------------------------


def _scratch_new_cmd_env(tmp_path, monkeypatch, mocker, *, git: bool, image_exists: bool = True):
    """`_setup_new_cmd_env` for a directory with no `.jailbee/config.yaml`.

    Builds on `_scratch_cwd` for the directory/XDG/`detect_default_branch`
    setup, then adds what `new_cmd` additionally needs: a mocked `Incus`,
    the docker-mirror pre-flight, and `lifecycle.new_container`.

    Returns `(new_container_mock, incus_mock)` — the `Incus` mock comes back
    so a test (or a later task's pre-flight test) can change
    `image_exists` / `profile_exists` without patching `jailbee.incus.Incus`
    a second time and losing this one's return values.
    """
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    _scratch_cwd(tmp_path, monkeypatch, mocker, git=git)

    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.image_exists.return_value = image_exists
    incus.profile_exists.return_value = True

    mirror_data_dir = tmp_path / "registry"
    (mirror_data_dir / "ca").mkdir(parents=True)
    (mirror_data_dir / "ca" / "ca.crt").write_text("fake-ca")
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(data_dir=mirror_data_dir)
        ),
    )
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.234.216.1", 3128),
    )
    mocker.patch("jailbee.apply.run_apply")

    new_container = mocker.patch("jailbee.lifecycle.new_container")
    new_container.return_value = "tutkimus-work"
    return new_container, incus


def test_new_in_a_non_git_scratch_dir_requires_mount(tmp_path, monkeypatch, mocker):
    """Clone mode has nothing to clone in a scratch directory with no
    `.git/` — `jb new` must refuse early with actionable advice, rather than
    let the clone fail deep inside container creation."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    _scratch_new_cmd_env(tmp_path, monkeypatch, mocker, git=False)

    result = CliRunner().invoke(app, ["new", "work"])
    # Rich wraps long lines at the runner's terminal width and may or may not
    # leave a trailing space at the break, so normalize all whitespace
    # (including newlines) to single spaces before matching a phrase that
    # spans a wrap point.
    collapsed = " ".join(result.output.split())

    assert result.exit_code == 2
    assert "not a git repository" in collapsed
    assert "--mount" in collapsed


def test_new_mount_in_a_non_git_scratch_dir_proceeds(tmp_path, monkeypatch, mocker):
    """--mount does not clone, so the same non-git scratch directory must be
    accepted rather than rejected by the new guard."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    new_container, _incus = _scratch_new_cmd_env(tmp_path, monkeypatch, mocker, git=False)

    result = CliRunner().invoke(app, ["new", "--mount", "work", "--no-autostart"])

    assert result.exit_code == 0, result.output
    assert new_container.call_count == 1
