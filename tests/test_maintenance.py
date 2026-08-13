"""Unit tests for maintenance.gather_disk_usage / _du_bytes / humanize.

Regression cover for the `gie disk-usage` "0.0 B" bug (root cause: the code
ran `du` on root-only /var/lib/incus paths and on symlinks it wouldn't
follow). Golden-image sizes now come from the Incus API; container sizes are
measured on the storage pool's real source path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from jailbee.global_config import DockerRegistryMirror, GlobalConfig
from jailbee.maintenance import (
    DiskRow,
    _du_bytes,
    gather_disk_usage,
    humanize,
)


def _row(rows: list[DiskRow], component: str) -> DiskRow:
    return next(r for r in rows if r.component == component)


def _incus(*, images=None, pools=None) -> MagicMock:
    incus = MagicMock()
    incus.list_images.return_value = images or []
    incus.list_storage_pools.return_value = pools or []
    return incus


def _gcfg(data_dir: Path) -> GlobalConfig:
    return GlobalConfig(docker_registry_mirror=DockerRegistryMirror(data_dir=data_dir))


# ---------- humanize ----------


def test_humanize_none_is_na():
    assert humanize(None) == "n/a"


def test_humanize_bytes():
    assert humanize(0) == "0.0 B"
    assert humanize(1536) == "1.5 KB"


# ---------- _du_bytes ----------


def test_du_bytes_zero_when_absent(tmp_path):
    assert _du_bytes(tmp_path / "does-not-exist") == 0


def test_du_bytes_measures_real_dir(tmp_path):
    (tmp_path / "f").write_bytes(b"x" * 4096)
    assert _du_bytes(tmp_path) >= 4096


def test_du_bytes_none_on_permission_error(tmp_path, mocker):
    # Present path, but `du` fails (e.g. root-only dir for the normal user
    # gie runs as) -> None ("n/a"), never a misleading 0.
    mocker.patch(
        "jailbee.maintenance.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "du", stderr="Permission denied"),
    )
    assert _du_bytes(tmp_path) is None


# ---------- gather_disk_usage: Golden images ----------


def test_golden_images_size_from_image_list_not_du(make_cfg, tmp_path):
    # The images row must come from summing incus.list_images() sizes,
    # independent of any filesystem du (which returns 0 as non-root).
    incus = _incus(
        images=[
            {"aliases": [{"name": "a-base"}], "size": 1000},
            {"aliases": [], "size": 2500},
        ]
    )
    cfg = make_cfg(tmp_path)
    rows = gather_disk_usage(cfg, _gcfg(tmp_path / "reg"), incus)
    assert _row(rows, "Golden images").size_bytes == 3500


# ---------- gather_disk_usage: Containers ----------


def test_containers_measured_on_pool_source_path(make_cfg, tmp_path):
    # Real container data lives under <pool.source>/containers, NOT the
    # symlink farm at /var/lib/incus/containers.
    pool = tmp_path / "pool"
    (pool / "containers" / "c1").mkdir(parents=True)
    (pool / "containers" / "c1" / "blob").write_bytes(b"y" * 8192)
    incus = _incus(pools=[{"name": "default", "driver": "dir", "config": {"source": str(pool)}}])
    cfg = make_cfg(tmp_path)
    rows = gather_disk_usage(cfg, _gcfg(tmp_path / "reg"), incus)
    assert _row(rows, "Containers").size_bytes >= 8192


def test_containers_na_when_pool_dir_unreadable(make_cfg, tmp_path, mocker):
    pool = tmp_path / "pool"
    (pool / "containers").mkdir(parents=True)
    incus = _incus(pools=[{"name": "default", "driver": "dir", "config": {"source": str(pool)}}])

    real_du = subprocess.run

    def fake_run(argv, *a, **kw):
        if "containers" in str(argv[-1]):
            raise subprocess.CalledProcessError(1, "du", stderr="Permission denied")
        return real_du(argv, *a, **kw)

    mocker.patch("jailbee.maintenance.subprocess.run", side_effect=fake_run)
    cfg = make_cfg(tmp_path)
    rows = gather_disk_usage(cfg, _gcfg(tmp_path / "reg"), incus)
    assert _row(rows, "Containers").size_bytes is None


def test_containers_zero_when_no_pools(make_cfg, tmp_path):
    incus = _incus(pools=[])
    cfg = make_cfg(tmp_path)
    rows = gather_disk_usage(cfg, _gcfg(tmp_path / "reg"), incus)
    assert _row(rows, "Containers").size_bytes == 0


# ---------- gather_disk_usage: user-owned rows still work ----------


def test_shared_caches_and_docker_rows_use_du(make_cfg, tmp_path):
    caches = tmp_path / "shared" / "caches"
    caches.mkdir(parents=True)
    (caches / "blob").write_bytes(b"z" * 2048)
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "blob").write_bytes(b"w" * 1024)
    incus = _incus()
    cfg = make_cfg(tmp_path, shared_dir=tmp_path / "shared")
    rows = gather_disk_usage(cfg, _gcfg(reg), incus)
    assert _row(rows, "Shared caches").size_bytes >= 2048
    assert _row(rows, "Docker registry mirror").size_bytes >= 1024
