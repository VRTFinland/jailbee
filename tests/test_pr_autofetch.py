from __future__ import annotations

from pathlib import Path

from jailbee.dashboard.model import RepoGroup
from jailbee.git_status import GitStatus
from jailbee.host_target import SyncTarget
from jailbee.lifecycle import ContainerInfo
from jailbee.pr_autofetch import AutofetchPolicy, PrHeadAutofetcher

ROOT = Path("/repos/alpha")


class _Time:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _container(name: str = "alpha-1", pr: int | None = 7, *, with_target: bool = True):
    gs = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        sync_target=SyncTarget("pr", f"PR #{pr}", "a" * 40, f"refs/jailbee/pr/{pr}/head")
        if with_target and pr is not None
        else None,
    )
    c = ContainerInfo(name, "Running", "strict", None, None, repo="alpha", git_status=gs)
    c.pr_number = pr
    return c


def _group(*containers: ContainerInfo, root: str | None = str(ROOT)) -> RepoGroup:
    return RepoGroup("alpha", root, None, list(containers))


class _Rig:
    """An autofetcher with a recording fetch, a movable clock and an editable policy."""

    def __init__(self, *, enabled: bool = True, interval: float = 300.0) -> None:
        self.mono = _Time()
        self.wall = _Time()
        self.wall.t = 1_700_000_000.0
        self.fetches: list[tuple[Path, int, str]] = []
        self.failure: Exception | None = None
        self.jobs: list = []
        self.policy = AutofetchPolicy(ROOT, "origin", enabled, interval)
        self.fetcher = PrHeadAutofetcher(
            policy=lambda root: self.policy,
            fetch=self._fetch,
            clock=self.mono,
            wall=self.wall,
            submit=lambda job: job(),
        )

    def _fetch(self, root: Path, number: int, remote: str) -> None:
        self.fetches.append((root, number, remote))
        if self.failure is not None:
            raise self.failure

    def annotate(self, *containers: ContainerInfo) -> None:
        self.fetcher.annotate([_group(*containers)])


def _target(c: ContainerInfo) -> SyncTarget:
    assert c.git_status is not None and c.git_status.sync_target is not None
    return c.git_status.sync_target


def test_a_bound_pr_is_fetched_and_the_fetch_age_lands_on_the_card_target():
    rig = _Rig()
    c = _container()

    rig.annotate(c)

    assert rig.fetches == [(ROOT, 7, "origin")]
    assert _target(c).fetched_at == 1_700_000_000.0
    assert _target(c).fetch_error is None
    assert _target(c).autofetch is True


def test_nothing_is_fetched_more_often_than_the_interval_per_pr():
    rig = _Rig(interval=300)
    rig.annotate(_container())
    rig.mono.t += 299
    rig.annotate(_container())
    assert len(rig.fetches) == 1

    rig.mono.t += 1
    rig.annotate(_container())
    assert len(rig.fetches) == 2


def test_two_containers_on_one_pr_share_one_fetch():
    rig = _Rig()
    rig.annotate(_container("alpha-1"), _container("alpha-2"))
    assert len(rig.fetches) == 1


def test_a_failure_is_recorded_and_not_retried_before_the_next_interval():
    rig = _Rig(interval=300)
    rig.annotate(_container())
    rig.failure = RuntimeError("Permission denied (publickey).")
    rig.mono.t += 300
    rig.wall.t += 300
    rig.annotate(_container())
    assert len(rig.fetches) == 2

    c = _container()
    rig.mono.t += 10
    rig.annotate(c)

    assert len(rig.fetches) == 2  # no early retry
    t = _target(c)
    assert t.fetch_error == "Permission denied (publickey)."
    assert t.fetch_error_at == 1_700_000_300.0
    assert t.fetched_at == 1_700_000_000.0  # the last good fetch is still known
    assert t.sha == "a" * 40  # the head is untouched


def test_a_later_success_clears_the_error():
    rig = _Rig(interval=300)
    rig.failure = RuntimeError("boom")
    rig.annotate(_container())
    rig.failure = None
    rig.mono.t += 300
    c = _container()
    rig.annotate(c)
    assert _target(c).fetch_error is None
    assert _target(c).fetch_error_at is None


def test_with_autofetch_off_nothing_is_fetched_and_the_target_says_so():
    rig = _Rig(enabled=False)
    c = _container()

    rig.annotate(c)

    assert rig.fetches == []
    assert _target(c).autofetch is False
    assert _target(c).fetched_at is None


def test_a_container_without_a_pr_is_left_alone():
    rig = _Rig()
    c = _container(pr=None)
    before = c.git_status

    rig.annotate(c)

    assert rig.fetches == []
    assert c.git_status is before


def test_a_pr_container_not_yet_probed_is_still_fetched():
    rig = _Rig()
    c = _container()
    c.git_status = None

    rig.annotate(c)

    assert len(rig.fetches) == 1


def test_a_repo_whose_config_cannot_be_read_is_skipped():
    rig = _Rig()
    rig.fetcher = PrHeadAutofetcher(
        policy=lambda root: None, fetch=rig._fetch, submit=lambda job: job()
    )
    c = _container()
    rig.annotate(c)
    assert rig.fetches == []


def test_an_orphan_group_is_skipped():
    rig = _Rig()
    rig.fetcher.annotate([_group(_container(), root=None)])
    assert rig.fetches == []


def test_a_fetch_still_running_is_not_started_twice():
    rig = _Rig(interval=30)
    pending: list = []
    rig.fetcher = PrHeadAutofetcher(
        policy=lambda root: rig.policy,
        fetch=rig._fetch,
        clock=rig.mono,
        wall=rig.wall,
        submit=pending.append,
    )
    rig.annotate(_container())
    rig.mono.t += 100
    rig.annotate(_container())
    assert len(pending) == 1

    pending[0]()
    rig.mono.t += 100
    rig.annotate(_container())
    assert len(pending) == 2


def test_the_policy_is_reread_only_every_half_minute():
    rig = _Rig()
    reads: list[Path] = []

    def policy(root: Path):
        reads.append(root)
        return rig.policy

    rig.fetcher = PrHeadAutofetcher(
        policy=policy, fetch=rig._fetch, clock=rig.mono, wall=rig.wall, submit=lambda job: job()
    )
    rig.annotate(_container())
    rig.mono.t += 5
    rig.annotate(_container())
    assert len(reads) == 1
    rig.mono.t += 30
    rig.annotate(_container())
    assert len(reads) == 2


def test_a_stale_failure_is_dropped_once_the_ref_moves_on():
    rig = _Rig(interval=30)
    rig.failure = RuntimeError("boom")
    c = _container()
    rig.annotate(c)
    assert _target(c).fetch_error == "boom"

    # a manual `jb pr fetch` moved the ref
    moved = _container()
    assert moved.git_status is not None and moved.git_status.sync_target is not None
    moved.git_status = replace_sha(moved.git_status, "b" * 40)
    rig.wall.t += 60
    rig.annotate(moved)

    t = _target(moved)
    assert t.fetch_error is None and t.fetch_error_at is None
    assert t.fetched_at == rig.wall.t


def test_a_failure_stays_while_the_ref_is_unchanged():
    rig = _Rig(interval=1000)
    rig.failure = RuntimeError("boom")
    rig.annotate(_container())
    c = _container()
    rig.annotate(c)
    assert _target(c).fetch_error == "boom"


def replace_sha(gs: GitStatus, sha: str) -> GitStatus:
    from dataclasses import replace

    assert gs.sync_target is not None
    return replace(gs, sync_target=replace(gs.sync_target, sha=sha))


def test_read_policy_maps_the_pr_block_of_the_repo_config(tmp_path):
    from jailbee.pr_autofetch import read_policy

    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text(
        "container_prefix: rp\npr:\n  autofetch: false\n  autofetch_interval: 90\n"
    )
    p = read_policy(tmp_path)
    assert p is not None
    assert (p.enabled, p.interval) == (False, 90.0)
    assert p.repo_root == tmp_path.resolve() or p.repo_root == tmp_path


def test_read_policy_defaults_and_remote_come_from_the_config(tmp_path, mocker, make_cfg):
    from jailbee.pr_autofetch import read_policy

    cfg = make_cfg(tmp_path, upstream_remote="upstream")
    mocker.patch("jailbee.config.loader.load_repo_config", return_value=cfg)
    p = read_policy(tmp_path)
    assert p == AutofetchPolicy(cfg.repo_root, "upstream", True, 300.0)


def test_read_policy_is_none_when_the_config_cannot_load(tmp_path, mocker):
    from jailbee.pr_autofetch import read_policy

    mocker.patch("jailbee.config.loader.load_repo_config", side_effect=ValueError("bad"))
    assert read_policy(tmp_path) is None


def test_the_real_thread_path_records_a_raising_fetch_and_frees_the_slot():
    import threading

    done = threading.Event()

    def fetch(root, number, remote):
        try:
            raise RuntimeError("net down")
        finally:
            done.set()

    mono = _Time()
    fetcher = PrHeadAutofetcher(
        policy=lambda root: AutofetchPolicy(ROOT, "origin", True, 30.0),
        fetch=fetch,
        clock=mono,
        wall=lambda: 42.0,
    )
    fetcher.annotate([_group(_container())])
    assert done.wait(5)
    state = fetcher._states[(ROOT, 7)]
    for _ in range(500):
        if not state.in_flight:
            break
        threading.Event().wait(0.01)
    assert state.in_flight is False
    assert state.error == "net down" and state.error_at == 42.0
