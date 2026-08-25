from unittest.mock import MagicMock

import pytest

from jailbee import submodules
from jailbee.incus import IncusError


def _exec_router(responses):
    """Return a side_effect that matches on a substring of the joined cmd.

    `responses` is a list of (substring, return-or-exception) pairs, tried
    in order. Unmatched calls return "".
    """

    def _side_effect(name, cmd, **kwargs):
        joined = " ".join(cmd)
        for needle, value in responses:
            if needle in joined:
                if isinstance(value, Exception):
                    raise value
                return value
        return ""

    return _side_effect


def test_init_no_gitmodules_does_nothing():
    incus = MagicMock()
    incus.exec.side_effect = _exec_router([("--get-regexp", IncusError("no .gitmodules"))])

    submodules.init_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, gid=1000
    )

    joined_calls = [" ".join(c.args[1]) for c in incus.exec.call_args_list]
    assert not any("submodule update" in j for j in joined_calls)
    assert not any("submodule sync" in j for j in joined_calls)


def test_init_single_submodule_runs_config_update_sync():
    incus = MagicMock()
    incus.exec.side_effect = _exec_router(
        [
            (
                "config -f /home/dev/repo/.gitmodules --get-regexp",
                "submodule.lib.path lib\n",
            ),
            (
                "config -f /home/dev/repo/lib/.gitmodules --get-regexp",
                IncusError("no nested .gitmodules"),
            ),
            ("test -e /mnt/host-source/lib/.git", ""),
        ]
    )

    submodules.init_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, gid=1000
    )

    joined = [" ".join(c.args[1]) for c in incus.exec.call_args_list]
    assert any("config submodule.lib.url /mnt/host-source/lib" in j for j in joined)
    assert any("protocol.file.allow=always submodule update --init -- lib" in j for j in joined)
    assert any("submodule sync -- lib" in j for j in joined)


def test_init_hard_fails_when_host_submodule_uninitialized():
    incus = MagicMock()
    incus.exec.side_effect = _exec_router(
        [
            (
                "config -f /home/dev/repo/.gitmodules --get-regexp",
                "submodule.lib.path lib\n",
            ),
            ("test -e /mnt/host-source/lib/.git", IncusError("missing")),
        ]
    )

    with pytest.raises(submodules.SubmoduleError, match="not initialized"):
        submodules.init_submodules_in_container(
            incus, "c1", repo_dir="/home/dev/repo", uid=1000, gid=1000
        )


def test_init_two_level_recursion():
    """Top-level has `lib`; `lib` itself has nested submodule `inner`."""
    incus = MagicMock()
    incus.exec.side_effect = _exec_router(
        [
            # Top-level .gitmodules: one submodule lib
            (
                "config -f /home/dev/repo/.gitmodules --get-regexp",
                "submodule.lib.path lib\n",
            ),
            # lib's .gitmodules: one nested submodule inner
            (
                "config -f /home/dev/repo/lib/.gitmodules --get-regexp",
                "submodule.inner.path inner\n",
            ),
            # inner has no further submodules
            (
                "config -f /home/dev/repo/lib/inner/.gitmodules --get-regexp",
                IncusError("no .gitmodules"),
            ),
            # host source existence checks
            ("test -e /mnt/host-source/lib/.git", ""),
            ("test -e /mnt/host-source/lib/inner/.git", ""),
        ]
    )

    submodules.init_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, gid=1000
    )

    joined = [" ".join(c.args[1]) for c in incus.exec.call_args_list]

    # Nested level: config/update/sync must target the inner submodule
    assert any("config submodule.inner.url /mnt/host-source/lib/inner" in j for j in joined)
    assert any("protocol.file.allow=always submodule update --init -- inner" in j for j in joined)
    assert any("submodule sync -- inner" in j for j in joined)

    # Nested commands must run with -C /home/dev/repo/lib
    assert any(
        "git -C /home/dev/repo/lib" in j and "config submodule.inner.url" in j for j in joined
    )
    assert any(
        "git -C /home/dev/repo/lib" in j and "submodule update --init -- inner" in j for j in joined
    )
    assert any("git -C /home/dev/repo/lib" in j and "submodule sync -- inner" in j for j in joined)


def test_update_in_container_runs_recursive_update():
    incus = MagicMock()
    incus.exec.return_value = ""
    submodules.update_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, env={"HOME": "/home/dev"}
    )
    joined_calls = [" ".join(c.args[1]) for c in incus.exec.call_args_list]
    assert any(
        "protocol.file.allow=always submodule update --init --recursive" in j for j in joined_calls
    )


def test_update_in_container_hard_fails_on_error():
    incus = MagicMock()
    incus.exec.side_effect = IncusError("missing object")
    with pytest.raises(submodules.SubmoduleError):
        submodules.update_submodules_in_container(
            incus, "c1", repo_dir="/home/dev/repo", uid=1000, env={}
        )


def test_update_on_host_delegates_to_git(mocker):
    from pathlib import Path

    upd = mocker.patch("jailbee.submodules.git.submodule_update")
    mocker.patch("jailbee.submodules.git.run_capture", return_value=(False, ""))
    submodules.update_submodules_on_host(Path("/host/repo"))
    upd.assert_called_once_with(Path("/host/repo"))


def test_update_on_host_hard_fails_on_git_error(mocker):
    from pathlib import Path

    from jailbee import git as gitmod

    mocker.patch(
        "jailbee.submodules.git.submodule_update",
        side_effect=gitmod.GitError("boom"),
    )
    with pytest.raises(submodules.SubmoduleError):
        submodules.update_submodules_on_host(Path("/host/repo"))


def _cfg_repo(tmp_path, *, upstream_remote="origin"):
    cfg = MagicMock()
    cfg.container_user.uid = 1000
    cfg.container_user.gid = 1000
    cfg.repo_root = tmp_path
    cfg.upstream_remote = upstream_remote
    return cfg


def test_transport_to_host_fetches_each_container_submodule(mocker, tmp_path):
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.return_value = " 1111 lib (v1)\n"
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=True)
    fetch = mocker.patch("jailbee.submodules.git.fetch_url_multi")
    clone = mocker.patch("jailbee.submodules.git.clone_url")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    clone.assert_not_called()
    assert fetch.call_count == 1
    repo_arg, url_arg, refspecs = fetch.call_args.args
    assert str(repo_arg) == str(tmp_path / "lib")
    assert "upload-pack /home/dev/repo/lib" in url_arg
    assert refspecs == [
        "+HEAD:refs/jailbee-sub/feat-x/lib/HEAD",
        "+refs/heads/*:refs/jailbee-sub/feat-x/lib/heads/*",
    ]


def test_transport_to_host_clones_missing_host_subrepo(mocker, tmp_path):
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.return_value = " 1111 lib (v1)\n"
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=False)
    fetch = mocker.patch("jailbee.submodules.git.fetch_url_multi")
    clone = mocker.patch("jailbee.submodules.git.clone_url")
    mocker.patch("jailbee.submodules._repoint_cloned_subrepo")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    clone.assert_called_once()
    url_arg, dest_arg = clone.call_args.args
    assert "upload-pack /home/dev/repo/lib" in url_arg
    assert str(dest_arg) == str(tmp_path / "lib")
    # A freshly cloned sub-repo is fetched into as well, so its
    # refs/jailbee-sub/<short>/<path>/{HEAD,heads/*} exist even though the
    # host had never seen this submodule before this call.
    fetch.assert_called_once()
    repo_arg, fetch_url_arg, refspecs = fetch.call_args.args
    assert str(repo_arg) == str(tmp_path / "lib")
    assert "upload-pack /home/dev/repo/lib" in fetch_url_arg
    assert refspecs == [
        "+HEAD:refs/jailbee-sub/feat-x/lib/HEAD",
        "+refs/heads/*:refs/jailbee-sub/feat-x/lib/heads/*",
    ]


def _container_exec_stub(status: str, gitmodules: dict[str, dict[str, str]]):
    """incus.exec stub answering `submodule status` and `.gitmodules` reads.

    `gitmodules` maps a level's `.gitmodules` path -> {submodule name: url};
    the declared path is taken to equal the name (enough for these tests).
    """

    def _exec(_name, cmd, **_kw):
        if cmd[:2] == ["git", "-C"] and "submodule" in cmd:
            return status
        if cmd[:3] == ["git", "config", "-f"]:
            entries = gitmodules.get(cmd[3])
            if entries is None:
                raise IncusError(f"no .gitmodules at {cmd[3]}")
            if "--get-regexp" in cmd:
                return "".join(f"submodule.{n}.path {n}\n" for n in entries)
            key = cmd[cmd.index("--get") + 1]
            name = key[len("submodule.") : -len(".url")]
            return f"{entries[name]}\n"
        return ""

    return _exec


def test_transport_to_host_points_a_cloned_subrepo_at_its_real_url(mocker, tmp_path):
    """Cloning from the container leaves `origin` on the ext:: URL — a remote
    that pushes into the container and dies with it. Repoint it at the URL the
    container's .gitmodules records, which is what every other clone of the
    superproject gets.
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _container_exec_stub(
        " 1111 lib (v1)\n",
        {"/home/dev/repo/.gitmodules": {"lib": "git@github.com:acme/lib.git"}},
    )
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=False)
    mocker.patch("jailbee.submodules.git.clone_url")
    mocker.patch("jailbee.submodules.git.fetch_url_multi")
    set_origin = mocker.patch("jailbee.submodules.git.set_origin_url")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    set_origin.assert_called_once_with(tmp_path / "lib", "git@github.com:acme/lib.git")


def test_transport_to_host_reads_a_nested_submodule_url_from_its_own_level(mocker, tmp_path):
    """A nested submodule's URL lives in its parent submodule's .gitmodules,
    keyed by the leaf path — not in the superproject's.
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _container_exec_stub(
        " 1111 lib/nested (v1)\n",
        {
            "/home/dev/repo/.gitmodules": {"lib": "git@github.com:acme/lib.git"},
            "/home/dev/repo/lib/.gitmodules": {"nested": "git@github.com:acme/nested.git"},
        },
    )
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=False)
    mocker.patch("jailbee.submodules.git.clone_url")
    mocker.patch("jailbee.submodules.git.fetch_url_multi")
    set_origin = mocker.patch("jailbee.submodules.git.set_origin_url")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    set_origin.assert_called_once_with(tmp_path / "lib/nested", "git@github.com:acme/nested.git")


def test_transport_to_host_leaves_origin_alone_when_no_url_is_recorded(mocker, tmp_path):
    """No .gitmodules entry to copy → leave the clone as git made it rather
    than inventing a remote.
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _container_exec_stub(" 1111 lib (v1)\n", {})
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=False)
    mocker.patch("jailbee.submodules.git.clone_url")
    mocker.patch("jailbee.submodules.git.fetch_url_multi")
    set_origin = mocker.patch("jailbee.submodules.git.set_origin_url")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    set_origin.assert_not_called()


def test_transport_to_host_never_rewrites_an_existing_subrepos_origin(mocker, tmp_path):
    """An existing host sub-repo is the user's own clone — its remote is not
    gie's to rewrite.
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _container_exec_stub(
        " 1111 lib (v1)\n",
        {"/home/dev/repo/.gitmodules": {"lib": "git@github.com:acme/lib.git"}},
    )
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodules.git.fetch_url_multi")
    set_origin = mocker.patch("jailbee.submodules.git.set_origin_url")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    set_origin.assert_not_called()


def test_transport_to_host_survives_a_failing_origin_rewrite(mocker, tmp_path):
    """The objects are already across when the remote is rewired; a failure
    there is cosmetic and must not fail the pull.
    """
    from jailbee.git import GitError

    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _container_exec_stub(
        " 1111 lib (v1)\n",
        {"/home/dev/repo/.gitmodules": {"lib": "git@github.com:acme/lib.git"}},
    )
    mocker.patch("jailbee.submodules._host_subrepo_exists", return_value=False)
    mocker.patch("jailbee.submodules.git.clone_url")
    mocker.patch("jailbee.submodules.git.fetch_url_multi")
    mocker.patch(
        "jailbee.submodules.git.set_origin_url",
        side_effect=GitError("git remote set-url failed (exit 1)"),
    )
    warn = mocker.patch("jailbee.submodules._warn")

    submodules.transport_submodules_to_host(
        cfg, incus, "full-c", "feat-x", repo_dir="/home/dev/repo"
    )

    assert warn.call_count == 1


def test_transport_to_container_pushes_each_host_submodule(mocker, tmp_path):
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    mocker.patch(
        "jailbee.submodules.git.submodule_status_paths",
        return_value=["lib", "lib/nested"],
    )
    push = mocker.patch("jailbee.submodules.git.push_url_multi")

    submodules.transport_submodules_to_container(cfg, incus, "full-c", repo_dir="/home/dev/repo")

    assert push.call_count == 2
    first_repo, first_url, first_refspecs = push.call_args_list[0].args
    assert str(first_repo) == str(tmp_path / "lib")
    assert "receive-pack /home/dev/repo/lib" in first_url
    assert first_refspecs == [
        "+HEAD:refs/jailbee-sub/host/lib/HEAD",
        "+refs/heads/*:refs/jailbee-sub/host/lib/heads/*",
    ]


def _exec_recorder(missing: set[str] | None = None):
    """incus.exec stub recording calls; `test -e <p>` fails for p in `missing`."""
    calls: list[list[str]] = []
    missing = missing or set()

    def _exec(_name, cmd, **_kw):
        calls.append(cmd)
        if cmd[:2] == ["test", "-e"] and cmd[2] in missing:
            raise IncusError(f"no such file: {cmd[2]}")
        return ""

    return calls, _exec


def test_transport_to_container_creates_a_subrepo_the_container_lacks(mocker, tmp_path):
    """A submodule added on the host has no repo in the container yet, so
    `git receive-pack <repo_dir>/<path>` dies with "does not appear to be a git
    repository". Create it first, then push into it, then give it a HEAD — the
    later `submodule update --init` needs a current revision to work from.
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    calls, exec_stub = _exec_recorder(missing={"/home/dev/repo/lib/.git"})
    incus.exec.side_effect = exec_stub
    mocker.patch("jailbee.submodules.git.submodule_status_paths", return_value=["lib"])
    mocker.patch("jailbee.submodules.git.get_remote_url", return_value=None)
    push = mocker.patch("jailbee.submodules.git.push_url_multi")

    submodules.transport_submodules_to_container(cfg, incus, "full-c", repo_dir="/home/dev/repo")

    init = next(c for c in calls if c[:2] == ["git", "init"])
    assert init[-1] == "/home/dev/repo/lib"
    detach = next(c for c in calls if "checkout" in c)
    assert detach == [
        "git",
        "-C",
        "/home/dev/repo/lib",
        "checkout",
        "--detach",
        "refs/jailbee-sub/host/lib/HEAD",
    ]
    # init before the push (the push needs a repo), detach after it (the
    # detach needs the ref the push just wrote).
    assert calls.index(init) < calls.index(detach)
    assert push.call_count == 1


def test_transport_to_container_points_a_new_subrepo_at_the_real_origin(mocker, tmp_path):
    """`git init` leaves no origin, so git inside the container could not
    fetch/push that submodule. Seed it from the host sub-repo's own origin —
    the same upstream `init_submodules_in_container` ends up with.
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    calls, exec_stub = _exec_recorder(missing={"/home/dev/repo/lib/.git"})
    incus.exec.side_effect = exec_stub
    mocker.patch("jailbee.submodules.git.submodule_status_paths", return_value=["lib"])
    origin = mocker.patch(
        "jailbee.submodules.git.get_remote_url",
        return_value="git@github.com:acme/lib.git",
    )
    mocker.patch("jailbee.submodules.git.push_url_multi")

    submodules.transport_submodules_to_container(cfg, incus, "full-c", repo_dir="/home/dev/repo")

    assert origin.call_args.args[0] == tmp_path / "lib"
    assert [
        "git",
        "-C",
        "/home/dev/repo/lib",
        "remote",
        "add",
        "origin",
        "git@github.com:acme/lib.git",
    ] in calls


def test_transport_to_container_resolves_the_submodules_own_remote_name(mocker, tmp_path):
    """A submodule is a separate repo and may name its upstream differently
    from the superproject — so the name is resolved against the submodule's own
    directory, not inherited from `cfg.upstream_remote`.
    """
    cfg = _cfg_repo(tmp_path, upstream_remote="public")
    incus = MagicMock()
    _calls, exec_stub = _exec_recorder(missing={"/home/dev/repo/lib/.git"})
    incus.exec.side_effect = exec_stub
    mocker.patch("jailbee.submodules.git.submodule_status_paths", return_value=["lib"])
    mocker.patch("jailbee.submodules.git.detect_upstream_remote", return_value="fork")
    url = mocker.patch(
        "jailbee.submodules.git.get_remote_url",
        return_value="git@github.com:acme/lib.git",
    )
    mocker.patch("jailbee.submodules.git.push_url_multi")

    submodules.transport_submodules_to_container(cfg, incus, "full-c", repo_dir="/home/dev/repo")

    assert url.call_args.args == (tmp_path / "lib", "fork")


def test_transport_to_container_never_touches_an_existing_subrepo(mocker, tmp_path):
    """The container's submodule may hold the user's own work — an existing
    sub-repo is pushed into and otherwise left alone (no init, no checkout).
    """
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    calls, exec_stub = _exec_recorder()
    incus.exec.side_effect = exec_stub
    mocker.patch("jailbee.submodules.git.submodule_status_paths", return_value=["lib"])
    push = mocker.patch("jailbee.submodules.git.push_url_multi")

    submodules.transport_submodules_to_container(cfg, incus, "full-c", repo_dir="/home/dev/repo")

    assert not any(c[:2] == ["git", "init"] for c in calls)
    assert not any("checkout" in c for c in calls)
    push.assert_called_once()


def test_prune_host_submodule_refs_deletes_each(mocker, tmp_path):
    cfg = _cfg_repo(tmp_path)
    mocker.patch("jailbee.submodules.git.submodule_status_paths", return_value=["lib"])
    mocker.patch(
        "jailbee.submodules.git.list_refs",
        return_value=["refs/jailbee-sub/feat-x/lib/HEAD"],
    )
    delete = mocker.patch("jailbee.submodules.git.delete_ref")

    submodules.prune_host_submodule_refs(cfg, "feat-x")

    delete.assert_called_once_with(tmp_path / "lib", "refs/jailbee-sub/feat-x/lib/HEAD")


def test_container_runner_success_returns_stdout():
    incus = MagicMock()
    incus.exec.return_value = "deadbeef\n"

    run = submodules._container_runner(incus, "c1", uid=1000, gid=1000)
    ok, out = run("/home/dev/repo/lib", ["rev-parse", "HEAD"])

    assert (ok, out) == (True, "deadbeef\n")
    incus.exec.assert_called_once_with(
        "c1",
        ["git", "-C", "/home/dev/repo/lib", "rev-parse", "HEAD"],
        uid=1000,
        gid=1000,
        env=None,
    )


def test_container_runner_maps_incus_error_to_false():
    incus = MagicMock()
    incus.exec.side_effect = IncusError("boom")

    run = submodules._container_runner(incus, "c1", uid=1000)
    assert run("/home/dev/repo/lib", ["status", "--porcelain"]) == (False, "")


def test_container_runner_passes_env():
    incus = MagicMock()
    incus.exec.return_value = ""

    run = submodules._container_runner(incus, "c1", uid=1000, env={"HOME": "/home/dev"})
    run("/home/dev/repo", ["status", "--porcelain"])

    incus.exec.assert_called_once_with(
        "c1",
        ["git", "-C", "/home/dev/repo", "status", "--porcelain"],
        uid=1000,
        gid=None,
        env={"HOME": "/home/dev"},
    )


class _FakeRun:
    """A GitRun stub keyed by git subcommand (args[0]).

    `results` maps a subcommand string to either a (ok, stdout) tuple or a
    callable(cwd, args) -> (ok, stdout). Unmatched subcommands return (True, "").
    Records every call in `.calls` as (cwd, args-list).
    """

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cwd, args):
        self.calls.append((cwd, list(args)))
        r = self.results.get(args[0], (True, ""))
        return r(cwd, args) if callable(r) else r

    def checkouts(self):
        return [(cwd, args) for cwd, args in self.calls if args[0] == "checkout"]


def test_place_one_clean_no_local_branch_checks_out():
    run = _FakeRun(
        {
            "status": (True, ""),  # clean
            "rev-parse": (True, "deadbeef\n"),
            "show-ref": (False, ""),  # local branch does not exist
        }
    )

    submodules._place_one(run, "/repo/lib", "master")

    assert run.checkouts() == [("/repo/lib", ["checkout", "-B", "master", "deadbeef"])]


def test_place_one_ff_ancestor_checks_out():
    run = _FakeRun(
        {
            "status": (True, ""),
            "rev-parse": (True, "deadbeef\n"),
            "show-ref": (True, ""),  # local branch exists
            "merge-base": (True, ""),  # is-ancestor -> ff is safe
        }
    )

    submodules._place_one(run, "/repo/lib", "master")

    assert run.checkouts() == [("/repo/lib", ["checkout", "-B", "master", "deadbeef"])]


def test_place_one_diverged_skips_and_warns(mocker):
    """Genuine divergence: neither the gitlink nor the branch is an ancestor of
    the other. Conservative bail to a detached HEAD."""
    warn = mocker.patch("jailbee.submodules._warn")
    run = _FakeRun(
        {
            "status": (True, ""),
            "rev-parse": (True, "deadbeef\n"),
            "show-ref": (True, ""),  # branch exists
            "merge-base": (False, ""),  # ancestry False in BOTH directions -> diverged
        }
    )

    submodules._place_one(run, "/repo/lib", "master")

    assert run.checkouts() == []
    warn.assert_called_once()


def test_place_one_branch_ahead_keeps_branch_and_warns(mocker):
    """Gitlink is a strict ancestor of the local branch (stale gitlink, e.g. a
    skipped superproject gitlink bump): keep the newer branch checked out rather
    than rewinding/detaching to the stale gitlink, and warn actionably."""
    warn = mocker.patch("jailbee.submodules._warn")

    def merge_base(cwd, args):
        # args: ["merge-base", "--is-ancestor", <ancestor>, <descendant>]
        ancestor, descendant = args[2], args[3]
        # branch 'master' is NOT an ancestor of gitlink 'deadbeef';
        # gitlink 'deadbeef' IS an ancestor of branch 'master' (branch is ahead).
        return (ancestor == "deadbeef" and descendant == "master", "")

    run = _FakeRun(
        {
            "status": (True, ""),
            "rev-parse": (True, "deadbeef\n"),
            "show-ref": (True, ""),  # branch exists
            "merge-base": merge_base,
        }
    )

    submodules._place_one(run, "/repo/lib", "master")

    # keeps the existing (ahead) branch checked out: plain checkout, never -B
    assert run.checkouts() == [("/repo/lib", ["checkout", "master"])]
    warn.assert_called_once()


def test_place_one_dirty_skips_and_warns(mocker):
    warn = mocker.patch("jailbee.submodules._warn")
    run = _FakeRun({"status": (True, " M file.txt\n")})  # dirty working tree

    submodules._place_one(run, "/repo/lib", "master")

    assert run.checkouts() == []
    warn.assert_called_once()


def test_place_one_rev_parse_fails_does_not_crash():
    run = _FakeRun({"status": (True, ""), "rev-parse": (False, "")})
    submodules._place_one(run, "/repo/lib", "master")  # must not raise
    assert run.checkouts() == []


def test_walk_no_branch_declared_does_not_check_out():
    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        return (False, "")  # submodule.lib.branch not set

    run = _FakeRun(
        {
            "config": config,
            "status": (True, ""),
            "rev-parse": (True, "sha\n"),
            "show-ref": (False, ""),
        }
    )

    submodules._place_submodule_branches(run, "/repo")

    assert run.checkouts() == []


def test_walk_branch_dot_is_skipped():
    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        return (True, ".\n")  # branch = .

    run = _FakeRun(
        {
            "config": config,
            "status": (True, ""),
            "rev-parse": (True, "sha\n"),
            "show-ref": (False, ""),
        }
    )

    submodules._place_submodule_branches(run, "/repo")

    assert run.checkouts() == []


def test_walk_places_branch_per_level():
    def config(cwd, args):
        if "--get-regexp" in args:
            if cwd == "/repo":
                return (True, "submodule.lib.path lib\n")
            if cwd == "/repo/lib":
                return (True, "submodule.inner.path inner\n")
            return (False, "")
        # branch declarations, read from the parent level's .gitmodules
        if args[-1] == "submodule.lib.branch":
            return (True, "main\n")
        if args[-1] == "submodule.inner.branch":
            return (True, "dev\n")
        return (False, "")

    run = _FakeRun(
        {
            "config": config,
            "status": (True, ""),
            "rev-parse": (True, "sha\n"),
            "show-ref": (False, ""),
        }
    )

    submodules._place_submodule_branches(run, "/repo")

    assert ("/repo/lib", ["checkout", "-B", "main", "sha"]) in run.checkouts()
    assert ("/repo/lib/inner", ["checkout", "-B", "dev", "sha"]) in run.checkouts()


def test_walk_with_branch_places_all_recursively():
    """With a target branch, every submodule (even undeclared, even nested) is placed."""

    def config(cwd, args):
        if "--get-regexp" in args:
            if cwd == "/repo":
                return (True, "submodule.lib.path lib\n")
            if cwd == "/repo/lib":
                return (True, "submodule.inner.path inner\n")
            return (False, "")
        return (False, "")  # NO submodule.<name>.branch declarations anywhere

    run = _FakeRun(
        {
            "config": config,
            "status": (True, ""),
            "rev-parse": (True, "sha\n"),
            "show-ref": (False, ""),
        }
    )

    submodules._place_submodule_branches(run, "/repo", "feat/foo")

    assert ("/repo/lib", ["checkout", "-B", "feat/foo", "sha"]) in run.checkouts()
    assert ("/repo/lib/inner", ["checkout", "-B", "feat/foo", "sha"]) in run.checkouts()


def test_walk_none_branch_keeps_legacy_gitmodules_behaviour():
    """branch=None (host caller) still places only declared submodules on their
    declared branch — undeclared ones are left alone."""

    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        return (False, "")  # submodule.lib.branch not set

    run = _FakeRun(
        {
            "config": config,
            "status": (True, ""),
            "rev-parse": (True, "sha\n"),
            "show-ref": (False, ""),
        }
    )

    submodules._place_submodule_branches(run, "/repo", None)

    assert run.checkouts() == []


def test_update_in_container_passes_container_branch(mocker):
    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    incus = MagicMock()
    incus.exec.return_value = ""
    incus.config_get.return_value = "feat/foo"

    submodules.update_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, env={"HOME": "/home/dev"}
    )

    incus.config_get.assert_called_once_with("c1", "user.jailbee.branch")
    assert place.call_args.args[1] == "/home/dev/repo"
    assert place.call_args.args[2] == "feat/foo"


def test_update_in_container_uses_branch_override(mocker):
    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    incus = MagicMock()
    incus.exec.return_value = ""

    submodules.update_submodules_in_container(
        incus,
        "c1",
        repo_dir="/home/dev/repo",
        uid=1000,
        env={"HOME": "/home/dev"},
        branch="feat/override",
    )

    incus.config_get.assert_not_called()  # override supplied -> no label lookup
    assert place.call_args.args[1] == "/home/dev/repo"
    assert place.call_args.args[2] == "feat/override"


def test_report_submodule_branches_flat_and_nested():
    def config(cwd, args):
        if "--get-regexp" in args:
            if cwd == "/repo":
                return (True, "submodule.lib.path lib\n")
            if cwd == "/repo/lib":
                return (True, "submodule.inner.path inner\n")
            return (False, "")
        return (False, "")

    run = _FakeRun(
        {
            "config": config,
            # /repo/lib on a branch; /repo/lib/inner detached (empty symbolic-ref)
            "symbolic-ref": lambda cwd, args: (
                (True, "feat/foo\n") if cwd == "/repo/lib" else (False, "")
            ),
        }
    )

    report = submodules.report_submodule_branches(run, "/repo")

    assert report == [("lib", "feat/foo"), ("lib/inner", None)]


def test_init_invokes_placement(mocker):
    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    incus = MagicMock()
    incus.exec.side_effect = _exec_router([("--get-regexp", IncusError("no .gitmodules"))])

    submodules.init_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, gid=1000
    )

    place.assert_called_once()
    assert callable(place.call_args.args[0])
    assert place.call_args.args[1] == "/home/dev/repo"


def test_init_places_on_container_branch_and_seeds(mocker):
    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    seed = mocker.patch("jailbee.submodules.seed_submodule_base_anchors")
    incus = MagicMock()
    incus.exec.side_effect = _exec_router([("--get-regexp", IncusError("no .gitmodules"))])

    submodules.init_submodules_in_container(
        incus,
        "c1",
        repo_dir="/home/dev/repo",
        uid=1000,
        gid=1000,
        branch="feat/foo",
        base_branch="main",
    )

    assert place.call_args.args[1] == "/home/dev/repo"
    assert place.call_args.args[2] == "feat/foo"
    seed.assert_called_once()
    assert seed.call_args.args[1] == "/home/dev/repo"
    assert seed.call_args.kwargs["base_branch"] == "main"
    assert seed.call_args.kwargs["container_branch"] == "feat/foo"


def test_init_without_branch_skips_seeding(mocker):
    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    seed = mocker.patch("jailbee.submodules.seed_submodule_base_anchors")
    incus = MagicMock()
    incus.exec.side_effect = _exec_router([("--get-regexp", IncusError("no .gitmodules"))])

    submodules.init_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, gid=1000
    )

    seed.assert_not_called()
    assert place.call_args.args[2] is None


def test_update_in_container_invokes_placement(mocker):
    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    incus = MagicMock()
    incus.exec.return_value = ""

    submodules.update_submodules_in_container(
        incus, "c1", repo_dir="/home/dev/repo", uid=1000, env={"HOME": "/home/dev"}
    )

    place.assert_called_once()
    assert callable(place.call_args.args[0])
    assert place.call_args.args[1] == "/home/dev/repo"


def test_update_on_host_invokes_placement_with_none_by_default(mocker):
    from pathlib import Path

    from jailbee import git as gitmod

    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    mocker.patch("jailbee.submodules.git.submodule_update")

    submodules.update_submodules_on_host(Path("/host/repo"))

    place.assert_called_once()
    assert place.call_args.args[0] is gitmod.run_capture
    assert place.call_args.args[1] == "/host/repo"
    assert place.call_args.args[2] is None  # no branch -> legacy placement


def test_update_on_host_forwards_branch(mocker):
    from pathlib import Path

    place = mocker.patch("jailbee.submodules._place_submodule_branches")
    mocker.patch("jailbee.submodules.git.submodule_update")

    submodules.update_submodules_on_host(Path("/host/repo"), branch="feat/foo")

    assert place.call_args.args[1] == "/host/repo"
    assert place.call_args.args[2] == "feat/foo"


def test_unmerged_entries_parses_stages():
    lsfiles = (
        "160000 1111111111111111111111111111111111111111 1\tlib/foo\n"
        "160000 2222222222222222222222222222222222222222 2\tlib/foo\n"
        "160000 3333333333333333333333333333333333333333 3\tlib/foo\n"
        "100644 4444444444444444444444444444444444444444 2\tREADME.md\n"
        "100644 5555555555555555555555555555555555555555 3\tREADME.md\n"
    )
    run = _FakeRun({"ls-files": (True, lsfiles)})
    entries = submodules._unmerged_entries(run, "/repo")
    assert entries["lib/foo"][2] == ("160000", "2222222222222222222222222222222222222222")
    assert entries["lib/foo"][3] == ("160000", "3333333333333333333333333333333333333333")
    assert entries["README.md"][2] == ("100644", "4444444444444444444444444444444444444444")


def test_unmerged_entries_empty_when_clean():
    run = _FakeRun({"ls-files": (True, "")})
    assert submodules._unmerged_entries(run, "/repo") == {}


def test_has_unmerged_true_and_false():
    run = _FakeRun({"ls-files": (True, "160000 abc 2\tlib\n")})
    assert submodules._has_unmerged(run, "/repo") is True
    assert submodules._has_unmerged(_FakeRun({"ls-files": (True, "")}), "/repo") is False
    # malformed line (no stage triple) is ignored, not counted
    assert submodules._has_unmerged(_FakeRun({"ls-files": (True, "garbage\n")}), "/repo") is False


def test_conflicted_gitlinks_returns_ours_theirs():
    lsfiles = (
        "160000 aaaa 1\tlib/foo\n"
        "160000 bbbb 2\tlib/foo\n"
        "160000 cccc 3\tlib/foo\n"
        "100644 dddd 2\tREADME.md\n"
        "100644 eeee 3\tREADME.md\n"
    )
    run = _FakeRun({"ls-files": (True, lsfiles)})
    assert submodules._conflicted_gitlinks(run, "/repo") == [("lib/foo", "bbbb", "cccc")]


def test_conflicted_gitlinks_deleted_side_has_none():
    lsfiles = "160000 aaaa 1\tlib/foo\n160000 bbbb 2\tlib/foo\n"  # no stage 3
    run = _FakeRun({"ls-files": (True, lsfiles)})
    assert submodules._conflicted_gitlinks(run, "/repo") == [("lib/foo", "bbbb", None)]


def test_nongitlink_unmerged_paths_excludes_gitlinks():
    lsfiles = (
        "160000 bbbb 2\tlib/foo\n"
        "160000 cccc 3\tlib/foo\n"
        "100644 dddd 2\tREADME.md\n"
        "100644 eeee 3\tREADME.md\n"
    )
    run = _FakeRun({"ls-files": (True, lsfiles)})
    assert submodules._nongitlink_unmerged_paths(run, "/repo") == ["README.md"]


def test_resolve_no_conflicts_is_noop():
    run = _FakeRun({"ls-files": (True, "")})
    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")
    assert report.resolved == []
    assert report.unresolved == []
    assert [c for c in run.calls if c[1][0] == "merge"] == []


def test_resolve_clean_merge_stages_pointer():
    def ls_files(cwd, args):
        # top has a gitlink conflict; the submodule itself is clean
        if cwd == "/repo":
            return (True, "160000 ours 2\tlib\n160000 theirs 3\tlib\n")
        return (True, "")

    run = _FakeRun(
        {
            "ls-files": ls_files,
            "status": (True, ""),  # submodule not dirty
            "checkout": (True, ""),
            "merge": (True, "Merge made by the 'ort' strategy.\n"),
            "config": (False, ""),  # no nested .gitmodules
            "add": (True, ""),
        }
    )

    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")

    assert report.resolved == ["lib"]
    assert report.unresolved == []
    assert ("/repo/lib", ["merge", "--no-edit", "-m", "m", "theirs"]) in run.calls
    assert ("/repo", ["add", "lib"]) in run.calls


def test_resolve_detaches_to_ours_before_merge():
    def ls_files(cwd, args):
        return (
            (True, "160000 ours 2\tlib\n160000 theirs 3\tlib\n") if cwd == "/repo" else (True, "")
        )

    run = _FakeRun(
        {
            "ls-files": ls_files,
            "status": (True, ""),
            "checkout": (True, ""),
            "merge": (True, ""),
            "config": (False, ""),
            "add": (True, ""),
        }
    )
    submodules.resolve_gitlink_conflicts(run, "/repo", message="m")
    assert ("/repo/lib", ["checkout", "--detach", "ours"]) in run.calls


def test_resolve_content_conflict_keeps_output():
    def ls_files(cwd, args):
        return (
            (True, "160000 ours 2\tlib\n160000 theirs 3\tlib\n") if cwd == "/repo" else (True, "")
        )

    merge_out = "Auto-merging x.c\nCONFLICT (content): Merge conflict in x.c\n"
    run = _FakeRun(
        {
            "ls-files": ls_files,
            "status": (True, ""),
            "checkout": (True, ""),
            "merge": (False, merge_out),
        }
    )
    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")
    assert report.resolved == []
    assert len(report.unresolved) == 1
    u = report.unresolved[0]
    assert u.path == "lib" and u.reason == "content-conflict"
    assert "CONFLICT (content): Merge conflict in x.c" in u.output
    assert ("/repo", ["add", "lib"]) not in run.calls


def test_resolve_dirty_submodule_skipped():
    def ls_files(cwd, args):
        return (
            (True, "160000 ours 2\tlib\n160000 theirs 3\tlib\n") if cwd == "/repo" else (True, "")
        )

    run = _FakeRun({"ls-files": ls_files, "status": (True, " M f\n")})  # dirty
    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")
    assert report.unresolved[0].reason == "dirty"
    assert [c for c in run.calls if c[1][0] == "merge"] == []


def test_resolve_deleted_side_reported():
    def ls_files(cwd, args):
        return (True, "160000 ours 2\tlib\n") if cwd == "/repo" else (True, "")  # no stage 3

    run = _FakeRun({"ls-files": ls_files})
    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")
    assert report.unresolved[0].reason == "deleted-side"
    assert [c for c in run.calls if c[1][0] == "merge"] == []


def test_resolve_attempts_all_no_failfast():
    def ls_files(cwd, args):
        if cwd == "/repo":
            return (True, "160000 oa 2\ta\n160000 ta 3\ta\n160000 ob 2\tb\n160000 tb 3\tb\n")
        return (True, "")

    def merge(cwd, args):
        return (False, "CONFLICT\n") if cwd == "/repo/a" else (True, "ok\n")

    run = _FakeRun(
        {
            "ls-files": ls_files,
            "status": (True, ""),
            "checkout": (True, ""),
            "merge": merge,
            "config": (False, ""),
            "add": (True, ""),
        }
    )
    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")
    assert report.resolved == ["b"]
    assert [u.path for u in report.unresolved] == ["a"]


def test_gitlink_at_parses_gitlink():
    run = _FakeRun({"ls-tree": (True, "160000 commit abc123\tlib\n")})
    assert submodules._gitlink_at(run, "/repo", "refs/jailbee/base/main", "lib") == "abc123"


def test_gitlink_at_non_gitlink_returns_none():
    run = _FakeRun({"ls-tree": (True, "100644 blob abc123\tfile.txt\n")})
    assert submodules._gitlink_at(run, "/repo", "X", "file.txt") is None


def test_gitlink_at_failed_lookup_returns_none():
    run = _FakeRun({"ls-tree": (False, "")})
    assert submodules._gitlink_at(run, "/repo", "X", "lib") is None


def test_detect_default_prefers_gitmodules_branch():
    run = _FakeRun({"config": (True, "release\n")})
    assert submodules._detect_submodule_default(run, "/repo", "/repo/lib", "lib") == "release"


def test_detect_default_falls_back_to_origin_head():
    run = _FakeRun({"config": (False, ""), "symbolic-ref": (True, "origin/trunk\n")})
    assert submodules._detect_submodule_default(run, "/repo", "/repo/lib", "lib") == "trunk"


def test_detect_default_final_fallback_main():
    run = _FakeRun({"config": (False, ""), "symbolic-ref": (False, "")})
    assert submodules._detect_submodule_default(run, "/repo", "/repo/lib", "lib") == "main"


def test_seed_pins_both_anchors():
    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        if args[-1] == "submodule.lib.branch":
            return (True, "master\n")
        return (False, "")

    run = _FakeRun({"config": config, "ls-tree": (True, "160000 commit deadbeef\tlib\n")})

    submodules.seed_submodule_base_anchors(
        run, "/repo", base_branch="main", container_branch="feat/foo"
    )

    updates = [(cwd, args) for cwd, args in run.calls if args[0] == "update-ref"]
    assert ("/repo/lib", ["update-ref", "refs/jailbee/base/main", "deadbeef"]) in updates
    assert ("/repo/lib", ["update-ref", "refs/heads/master", "deadbeef"]) in updates


def test_seed_skips_local_default_when_equal_to_container_branch():
    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        if args[-1] == "submodule.lib.branch":
            return (True, "feat/foo\n")  # submodule default == container branch
        return (False, "")

    run = _FakeRun({"config": config, "ls-tree": (True, "160000 commit dead\tlib\n")})

    submodules.seed_submodule_base_anchors(
        run, "/repo", base_branch="main", container_branch="feat/foo"
    )

    updates = [args for _cwd, args in run.calls if args[0] == "update-ref"]
    assert ["update-ref", "refs/jailbee/base/main", "dead"] in updates
    assert ["update-ref", "refs/heads/feat/foo", "dead"] not in updates


def test_seed_skips_unresolvable_gitlink():
    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        return (False, "")

    run = _FakeRun({"config": config, "ls-tree": (False, "")})  # base ref/path missing

    submodules.seed_submodule_base_anchors(
        run, "/repo", base_branch="main", container_branch="feat/foo"
    )

    assert [c for c in run.calls if c[1][0] == "update-ref"] == []


def test_resolve_nested_gitlink_conflict_recurses_and_commits():
    # `git merge` exits non-zero when its only conflict is a gitlink, so the
    # submodule's own merge stops half-done: resolving `inner` must finish it.
    staged: set[str] = set()

    def ls_files(cwd, args):
        if cwd == "/repo":
            return (True, "160000 ours 2\tlib\n160000 theirs 3\tlib\n")
        if cwd == "/repo/lib" and "inner" not in staged:
            return (True, "160000 io 2\tinner\n160000 it 3\tinner\n")
        return (True, "")

    def merge(cwd, args):
        if cwd == "/repo/lib":
            return (False, "CONFLICT (submodule): Merge conflict in inner\n")
        return (True, "ok\n")

    def add(cwd, args):
        if cwd == "/repo/lib":
            staged.add(args[1])
        return (True, "")

    run = _FakeRun(
        {
            "ls-files": ls_files,
            "status": (True, ""),
            "checkout": (True, ""),
            "merge": merge,
            "add": add,
            "commit": (True, ""),
        }
    )

    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")

    assert report.resolved == ["lib/inner", "lib"]
    assert report.unresolved == []
    assert ("/repo/lib/inner", ["merge", "--no-edit", "-m", "m", "it"]) in run.calls
    assert ("/repo/lib", ["commit", "--no-edit"]) in run.calls
    assert ("/repo", ["add", "lib"]) in run.calls


def test_resolve_nested_conflict_bubbles_up():
    def ls_files(cwd, args):
        if cwd == "/repo":
            return (True, "160000 ours 2\tlib\n160000 theirs 3\tlib\n")
        if cwd == "/repo/lib":
            # the submodule's merge stopped on ITS own gitlink (inner)
            return (True, "160000 io 2\tinner\n160000 it 3\tinner\n")
        if cwd == "/repo/lib/inner":
            return (True, "100644 a 2\tx.c\n100644 b 3\tx.c\n")
        return (True, "")

    def merge(cwd, args):
        if cwd == "/repo/lib":
            return (False, "CONFLICT (submodule): Merge conflict in inner\n")
        if cwd == "/repo/lib/inner":
            return (False, "CONFLICT (content): Merge conflict in x.c\n")
        return (True, "ok\n")

    run = _FakeRun(
        {
            "ls-files": ls_files,
            "status": (True, ""),
            "checkout": (True, ""),
            "merge": merge,
            "add": (True, ""),
        }
    )
    report = submodules.resolve_gitlink_conflicts(run, "/repo", message="m")

    assert report.resolved == []
    assert [(u.path, u.reason) for u in report.unresolved] == [
        ("lib/inner", "content-conflict"),
        ("lib", "nested-conflict"),
    ]
    assert "CONFLICT (content): Merge conflict in x.c" in report.unresolved[0].output
    assert ("/repo", ["add", "lib"]) not in run.calls
    assert [c for c in run.calls if c[1][0] == "commit"] == []


# ---- delete_submodule_base_anchors ----------------------------------------


def test_delete_submodule_base_anchors_recursive():
    def config(cwd, args):
        if "--get-regexp" in args:
            if cwd == "/repo":
                return (True, "submodule.lib.path lib\n")
            if cwd == "/repo/lib":
                return (True, "submodule.inner.path inner\n")
            return (False, "")
        return (False, "")

    run = _FakeRun({"config": config})

    submodules.delete_submodule_base_anchors(run, "/repo", "main")

    deletes = [(cwd, args) for cwd, args in run.calls if args[:2] == ["update-ref", "-d"]]
    assert ("/repo/lib", ["update-ref", "-d", "refs/jailbee/base/main"]) in deletes
    assert ("/repo/lib/inner", ["update-ref", "-d", "refs/jailbee/base/main"]) in deletes


# ---- seed_submodule_base_anchors: FF-only guard for local default branch ----


def test_seed_local_default_not_rewound_when_b_sub_not_ff():
    """Local default branch must NOT be updated when b_sub is not a fast-forward
    of the current tip (i.e. _is_ancestor(default, b_sub) is False). The
    refs/jailbee/base/<base> anchor must still be set unconditionally."""

    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        if args[-1] == "submodule.lib.branch":
            return (True, "main\n")
        return (False, "")

    def show_ref(cwd, args):
        # Branch 'main' exists in the submodule.
        return (True, "deadbeef refs/heads/main\n")

    def merge_base(cwd, args):
        # args: ["merge-base", "--is-ancestor", <ancestor>, <descendant>]
        # b_sub ("OLDSUB") is NOT a fast-forward of "main" -> ancestor check False.
        return (False, "")

    run = _FakeRun(
        {
            "config": config,
            "ls-tree": (True, "160000 commit OLDSUB\tlib\n"),
            "show-ref": show_ref,
            "merge-base": merge_base,
            "symbolic-ref": (False, ""),
        }
    )

    submodules.seed_submodule_base_anchors(
        run, "/repo", base_branch="main", container_branch="feat/bar"
    )

    updates = [(cwd, args) for cwd, args in run.calls if args[0] == "update-ref"]
    # The gie anchor is always set.
    assert ("/repo/lib", ["update-ref", "refs/jailbee/base/main", "OLDSUB"]) in updates
    # The local default branch must NOT be rewound.
    assert ("/repo/lib", ["update-ref", "refs/heads/main", "OLDSUB"]) not in updates


def test_seed_local_default_created_when_absent():
    """Local default branch is set when it does not exist yet (absent = safe to create)."""

    def config(cwd, args):
        if "--get-regexp" in args:
            return (True, "submodule.lib.path lib\n") if cwd == "/repo" else (False, "")
        if args[-1] == "submodule.lib.branch":
            return (True, "main\n")
        return (False, "")

    def show_ref(cwd, args):
        # Branch 'main' does not exist.
        return (False, "")

    run = _FakeRun(
        {
            "config": config,
            "ls-tree": (True, "160000 commit NEWSUB\tlib\n"),
            "show-ref": show_ref,
            "symbolic-ref": (False, ""),
        }
    )

    submodules.seed_submodule_base_anchors(
        run, "/repo", base_branch="main", container_branch="feat/bar"
    )

    updates = [(cwd, args) for cwd, args in run.calls if args[0] == "update-ref"]
    assert ("/repo/lib", ["update-ref", "refs/jailbee/base/main", "NEWSUB"]) in updates
    assert ("/repo/lib", ["update-ref", "refs/heads/main", "NEWSUB"]) in updates


# ---- seed_submodule_base_anchors: two-level recursive seeding ---------------


def test_seed_recurses_into_nested_submodule():
    """_seed_level recurses into a nested submodule and seeds it using the gitlink
    from the parent submodule's object store, not the superproject's."""

    def config(cwd, args):
        if "--get-regexp" in args:
            if cwd == "/repo":
                return (True, "submodule.lib.path lib\n")
            if cwd == "/repo/lib":
                return (True, "submodule.inner.path inner\n")
            return (False, "")
        # No submodule.<name>.branch declarations.
        return (False, "")

    def ls_tree(cwd, args):
        # args: ["ls-tree", <commit>, "--", <path>]
        commit, path = args[1], args[3]
        if cwd == "/repo" and commit == "refs/jailbee/base/main" and path == "lib":
            return (True, "160000 commit AAAAAA\tlib\n")
        if cwd == "/repo/lib" and commit == "AAAAAA" and path == "inner":
            return (True, "160000 commit BBBBBB\tinner\n")
        return (False, "")

    # symbolic-ref returns (False, "") -> _detect_submodule_default falls back to "main".
    # show-ref default (True, "") -> _local_branch_exists returns True.
    # merge-base default (True, "") -> _is_ancestor returns True (FF is safe).
    run = _FakeRun(
        {
            "config": config,
            "ls-tree": ls_tree,
            "symbolic-ref": (False, ""),
        }
    )

    submodules.seed_submodule_base_anchors(
        run, "/repo", base_branch="main", container_branch="feat/foo"
    )

    updates = [(cwd, args) for cwd, args in run.calls if args[0] == "update-ref"]
    # Parent submodule gets its base anchor from the superproject gitlink.
    assert ("/repo/lib", ["update-ref", "refs/jailbee/base/main", "AAAAAA"]) in updates
    # Nested submodule gets its base anchor resolved from the parent's object store.
    assert ("/repo/lib/inner", ["update-ref", "refs/jailbee/base/main", "BBBBBB"]) in updates


def test_transport_to_host_only_filters_to_one_submodule(mocker, tmp_path):
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _exec_router(
        [("submodule status --recursive", " abc lib/a heads\n def lib/b heads\n")]
    )
    (tmp_path / "lib" / "a").mkdir(parents=True)
    (tmp_path / "lib" / "a" / ".git").write_text("")
    (tmp_path / "lib" / "b").mkdir(parents=True)
    (tmp_path / "lib" / "b" / ".git").write_text("")
    fetch = mocker.patch("jailbee.submodules.git.fetch_url_multi")

    submodules.transport_submodules_to_host(
        cfg,
        incus,
        "c1",
        "feat-foo",
        repo_dir="/home/dev/repo",
        only="lib/b",
    )

    assert [str(call.args[0]) for call in fetch.call_args_list] == [str(tmp_path / "lib" / "b")]


def test_transport_to_host_fetches_the_jailbee_refs_after_cloning(mocker, tmp_path):
    cfg = _cfg_repo(tmp_path)
    incus = MagicMock()
    incus.exec.side_effect = _exec_router([("submodule status --recursive", " abc lib/a heads\n")])
    clone = mocker.patch("jailbee.submodules.git.clone_url")
    mocker.patch("jailbee.submodules._repoint_cloned_subrepo")
    fetch = mocker.patch("jailbee.submodules.git.fetch_url_multi")

    submodules.transport_submodules_to_host(cfg, incus, "c1", "feat-foo", repo_dir="/home/dev/repo")

    clone.assert_called_once()
    refspecs = fetch.call_args.args[2]
    assert "+HEAD:refs/jailbee-sub/feat-foo/lib/a/HEAD" in refspecs
    assert "+refs/heads/*:refs/jailbee-sub/feat-foo/lib/a/heads/*" in refspecs


def test_declared_branch_for_path_reads_the_gitmodules_entry():
    def run(cwd, args):
        joined = " ".join(args)
        if "--get-regexp" in joined:
            return (True, "submodule.lib.path lib\n")
        if "submodule.lib.branch" in joined:
            return (True, "release\n")
        return (False, "")

    assert submodules.declared_branch_for_path(run, "/repo", "lib") == "release"


def test_declared_branch_for_path_treats_dot_as_undeclared():
    def run(cwd, args):
        joined = " ".join(args)
        if "--get-regexp" in joined:
            return (True, "submodule.lib.path lib\n")
        if "submodule.lib.branch" in joined:
            return (True, ".\n")
        return (False, "")

    assert submodules.declared_branch_for_path(run, "/repo", "lib") is None


def test_declared_branch_for_path_returns_none_for_an_unknown_leaf():
    def run(cwd, args):
        if "--get-regexp" in " ".join(args):
            return (True, "submodule.lib.path lib\n")
        return (False, "")

    assert submodules.declared_branch_for_path(run, "/repo", "other") is None
