"""Behaviour of the ensure-pi.sh provision script, run in a real bash.

PATH holds only the tools the script uses plus two stubs, so a `node` on the
test machine cannot leak in. `npm view` prints `$LATEST` (or fails when it is
unset); `npm install` logs the call and drops an executable `bin/pi` and a
`package.json` declaring `$ENGINES` under its `--prefix`, as the real package
does, unless `$FAIL_INSTALL` is set. `node` reports `$NODE_VERSION` and reads
`engines.node` back out of a manifest.
"""

import shutil
import subprocess

import pytest

from jailbee.agents import _resolve_bundled

PKG = "@earendil-works/pi-coding-agent"

_TOOLS = (
    "bash", "basename", "chmod", "flock", "grep", "head", "ln", "ls",
    "mkdir", "mktemp", "mv", "readlink", "rm", "sed", "sh", "sort", "tail",
)  # fmt: skip

_NPM = f"""#!/bin/sh
echo "$*" >> "$NPM_LOG"
case "$1" in
view)
    [ -n "$LATEST" ] || exit 1
    echo "$LATEST"
    ;;
install)
    [ -z "$FAIL_INSTALL" ] || exit 1
    while [ "$1" != --prefix ]; do shift; done
    mkdir -p "$2/bin" "$2/lib/node_modules/{PKG}"
    printf '#!/bin/sh\\n' > "$2/bin/pi"
    chmod 755 "$2/bin/pi"
    printf '{{"engines": {{"node": "%s"}}}}' "$ENGINES" > "$2/lib/node_modules/{PKG}/package.json"
    ;;
esac
"""

_NODE = """#!/bin/sh
if [ "$2" = process.versions.node ]; then
    echo "$NODE_VERSION"
    exit 0
fi
[ -f "$3" ] || exit 1
sed -n 's/.*"node": *"\\([^"]*\\)".*/\\1/p' "$3"
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def _run(
    tmp_path,
    home,
    *,
    latest="1.1.0",
    auto_update=True,
    fail_install=False,
    node="24.1.0",
    engines=">=22.19.0",
):
    stub_bin = tmp_path / "stub-bin"
    if not stub_bin.exists():
        stub_bin.mkdir()
        for tool in _TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (stub_bin / tool).symlink_to(found)
        (stub_bin / "npm").write_text(_NPM)
        (stub_bin / "npm").chmod(0o755)
    node_stub = stub_bin / "node"
    node_stub.unlink(missing_ok=True)
    if node:
        node_stub.write_text(_NODE)
        node_stub.chmod(0o755)
    log = tmp_path / "npm.log"
    log.unlink(missing_ok=True)
    env = {
        "HOME": str(home),
        "PATH": str(stub_bin),
        "NPM_LOG": str(log),
        "JAILBEE_AUTO_UPDATE": "true" if auto_update else "false",
        "NODE_VERSION": node or "",
        "ENGINES": engines,
    }
    if latest:
        env["LATEST"] = latest
    if fail_install:
        env["FAIL_INSTALL"] = "1"
    result = subprocess.run(
        [str(stub_bin / "bash"), "-c", _resolve_bundled("__bundled__:ensure-pi.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _store(home):
    return home / ".local/share/pi"


def _seed(home, *versions, current, engines=">=22.19.0"):
    for v in versions:
        release = _store(home) / "releases" / v
        binary = release / "bin/pi"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        manifest = release / "lib/node_modules" / PKG / "package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f'{{"engines": {{"node": "{engines}"}}}}')
    (_store(home) / "current").symlink_to(f"releases/{current}")


def _linked_release(home):
    return (home / ".local/bin/pi").resolve().parent.parent.name


def test_empty_store_installs_the_latest_release_even_with_auto_update_off(tmp_path, home):
    result, calls = _run(tmp_path, home, auto_update=False)

    assert result.returncode == 0, result.stderr
    assert calls[0] == f"view {PKG} version"
    assert "--ignore-scripts" in calls[1]
    assert calls[1].endswith(f" {PKG}@1.1.0")
    assert _linked_release(home) == "1.1.0"


def test_populated_store_with_auto_update_off_only_links(tmp_path, home):
    """A second container of the repo must not touch the registry at all."""
    _seed(home, "1.0.3", current="1.0.3")

    result, calls = _run(tmp_path, home, auto_update=False)

    assert result.returncode == 0, result.stderr
    assert calls == []
    assert _linked_release(home) == "1.0.3"


def test_up_to_date_store_does_not_reinstall(tmp_path, home):
    _seed(home, "1.1.0", current="1.1.0")

    result, calls = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert calls == [f"view {PKG} version"]


def test_update_installs_beside_the_running_release(tmp_path, home):
    """A sibling container's pi is still loading chunks from 1.0.3."""
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert _linked_release(home) == "1.1.0"
    assert (_store(home) / "releases/1.0.3/bin/pi").exists()


def test_update_keeps_the_two_newest_releases(tmp_path, home):
    _seed(home, "1.0.2", "1.0.10", current="1.0.10")

    result, _calls = _run(tmp_path, home, latest="1.1.0")

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (_store(home) / "releases").iterdir()) == ["1.0.10", "1.1.0"]


def test_failed_update_still_links_the_existing_release(tmp_path, home):
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home, fail_install=True)

    assert result.returncode != 0
    assert _linked_release(home) == "1.0.3"
    assert [p.name for p in (_store(home) / "releases").iterdir()] == ["1.0.3"]


def test_unreachable_registry_on_a_populated_store_still_links(tmp_path, home):
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home, latest="")

    assert result.returncode != 0
    assert _linked_release(home) == "1.0.3"


@pytest.mark.parametrize("latest, fail_install", [("1.1.0", True), ("", False)])
def test_empty_store_fails_loudly(tmp_path, home, latest, fail_install):
    result, _calls = _run(tmp_path, home, latest=latest, fail_install=fail_install)

    assert result.returncode != 0
    assert not (home / ".local/bin/pi").exists()


def test_a_dead_runs_temp_prefix_is_cleared(tmp_path, home):
    _seed(home, "1.1.0", current="1.1.0")
    leftover = _store(home) / "releases/.tmp-abc123"
    leftover.mkdir()

    result, _calls = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert not leftover.exists()


def test_missing_node_is_named_rather_than_blamed_on_the_registry(tmp_path, home):
    result, calls = _run(tmp_path, home, node=None)

    assert result.returncode != 0
    assert calls == []
    assert "golden.stacks.node" in result.stderr
    assert "registry" not in result.stderr


def test_too_old_node_fails_the_install_it_would_otherwise_pass(tmp_path, home):
    """npm itself only warns EBADENGINE, so the install looks fine."""
    result, _calls = _run(tmp_path, home, node="20.11.1")

    assert result.returncode != 0
    assert "needs Node >= 22.19.0" in result.stderr
    assert "20.11.1" in result.stderr


def test_too_old_node_is_caught_on_a_link_only_container(tmp_path, home):
    """The store was filled from a newer image than this container's."""
    _seed(home, "1.0.3", current="1.0.3")

    result, calls = _run(tmp_path, home, auto_update=False, node="22.12.0")

    assert calls == []
    assert result.returncode != 0
    assert "needs Node >= 22.19.0" in result.stderr


def test_new_enough_node_passes(tmp_path, home):
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home, auto_update=False, node="22.19.0")

    assert result.returncode == 0, result.stderr


def test_an_engines_range_it_cannot_read_is_not_checked(tmp_path, home):
    _seed(home, "1.0.3", current="1.0.3", engines="^22.19.0 || >=24")

    result, _calls = _run(tmp_path, home, auto_update=False, node="20.0.0")

    assert result.returncode == 0, result.stderr
