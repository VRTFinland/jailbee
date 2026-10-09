"""Behaviour of the ensure-opencode.sh provision script, run in a real bash.

`curl` is a stub on PATH. For the version pointer it prints `$LATEST` as the
vendor's JSON (or fails when it is unset); for the installer URL it prints a
fake installer, or fails when `$INSTALLER_FAIL` is set. The fake installer logs
its arguments and drops `~/.opencode/bin/opencode` reporting the version it was
pinned to, as the real one does with `--version`.
"""

import os
import subprocess

import pytest

from jailbee.agents import _resolve_bundled

_CURL = """#!/bin/sh
for url in "$@"; do :; done
case "$url" in
*/latest/cli/npm)
    [ -n "$LATEST" ] || exit 22
    printf '{"version":"%s","package":"@opencode/cli"}' "$LATEST"
    ;;
*/v2/install)
    [ -z "$INSTALLER_FAIL" ] || exit 6
    cat "$FAKE_INSTALLER"
    ;;
esac
"""

_INSTALLER = """echo "$*" >> "$INSTALL_LOG"
v="$LATEST"
[ "$2" != --version ] || v="$3"
mkdir -p "$HOME/.opencode/bin"
printf '#!/bin/sh\\necho %s\\n' "$v" > "$HOME/.opencode/bin/opencode"
chmod 755 "$HOME/.opencode/bin/opencode"
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def _run(tmp_path, home, *, latest="1.2.0", auto_update=True, installer_fail=False):
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    (stub_bin / "curl").write_text(_CURL)
    (stub_bin / "curl").chmod(0o755)
    installer = tmp_path / "installer.sh"
    installer.write_text(_INSTALLER)
    log = tmp_path / "install.log"
    log.unlink(missing_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "FAKE_INSTALLER": str(installer),
        "INSTALL_LOG": str(log),
        "JAILBEE_AUTO_UPDATE": "true" if auto_update else "false",
    }
    if latest:
        env["LATEST"] = latest
    if installer_fail:
        env["INSTALLER_FAIL"] = "1"
    result = subprocess.run(
        ["bash", "-c", _resolve_bundled("__bundled__:ensure-opencode.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    installs = log.read_text().splitlines() if log.exists() else []
    return result, installs


def _seed(home, version):
    binary = home / ".opencode/bin/opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!/bin/sh\necho {version}\n")
    binary.chmod(0o755)


def _linked_version(home):
    return subprocess.run(
        [str(home / ".local/bin/opencode")], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_empty_store_installs_even_with_auto_update_off(tmp_path, home):
    result, installs = _run(tmp_path, home, auto_update=False)

    assert result.returncode == 0, result.stderr
    assert installs == ["--no-modify-path"]
    link = home / ".local/bin/opencode"
    assert link.is_symlink()
    assert link.resolve() == home / ".opencode/bin/opencode"


def test_populated_store_with_auto_update_off_only_links(tmp_path, home):
    _seed(home, "1.1.0")

    result, installs = _run(tmp_path, home, auto_update=False, installer_fail=True)

    assert result.returncode == 0, result.stderr
    assert installs == []
    assert _linked_version(home) == "1.1.0"


def test_up_to_date_store_skips_the_88mb_download(tmp_path, home):
    _seed(home, "1.2.0")

    result, installs = _run(tmp_path, home, installer_fail=True)

    assert result.returncode == 0, result.stderr
    assert installs == []


def test_update_pins_the_installer_to_the_version_it_compared(tmp_path, home):
    _seed(home, "1.1.0")

    result, installs = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert installs == ["--no-modify-path --version 1.2.0"]
    assert _linked_version(home) == "1.2.0"


def test_failed_update_still_links_the_existing_binary(tmp_path, home):
    _seed(home, "1.1.0")

    result, _installs = _run(tmp_path, home, installer_fail=True)

    assert result.returncode != 0
    assert _linked_version(home) == "1.1.0"


def test_unreachable_version_pointer_still_links(tmp_path, home):
    _seed(home, "1.1.0")

    result, installs = _run(tmp_path, home, latest="")

    assert result.returncode != 0
    assert installs == []
    assert _linked_version(home) == "1.1.0"


def test_empty_store_fails_loudly_when_the_download_fails(tmp_path, home):
    """`curl … | bash` exits 0 when curl fails — bash reads an empty script —
    so only pipefail and the trailing `-x` test catch it."""
    result, _installs = _run(tmp_path, home, installer_fail=True)

    assert result.returncode != 0
    assert not (home / ".local/bin/opencode").exists()
