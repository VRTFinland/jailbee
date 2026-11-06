"""Unit tests for scripts/test_audit_static.py (tiny fixture trees in tmp_path)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test_audit_static.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_audit_static", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_audit_static"] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load()


def _scan(tmp_path: Path, source: str) -> dict[str, list[str]]:
    (tmp_path / "test_x.py").write_text(source, encoding="utf-8")
    res = audit.scan(tmp_path, tmp_path)
    return {i.name: i.flags for i in res["tests"]}


def test_flags_assertion_free_test(tmp_path: Path) -> None:
    flags = _scan(tmp_path, "def test_a():\n    x = 1\n    print(x)\n")
    assert "no_assert" in flags["test_a"]


def test_raises_mock_assert_and_helper_are_not_assertion_free(tmp_path: Path) -> None:
    src = (
        "import pytest\n"
        "def assert_ok(v):\n    assert v\n"
        "def wrapper(v):\n    assert_ok(v)\n"
        "def test_r():\n    with pytest.raises(ValueError):\n        int('x')\n"
        "def test_m(mocker):\n    m = mocker.Mock()\n    m.assert_not_called()\n"
        "def test_h():\n    assert_ok(1)\n"
        "def test_t():\n    wrapper(1)\n"
        "def test_f():\n    pytest.fail('x')\n"
    )
    flags = _scan(tmp_path, src)
    for name in ("test_r", "test_m", "test_h", "test_t", "test_f"):
        assert "no_assert" not in flags[name], name


def test_snapshot_fixture_counts_as_assertion(tmp_path: Path) -> None:
    flags = _scan(tmp_path, "def test_s(snap_compare):\n    snap_compare('a.py')\n")
    assert "no_assert" not in flags["test_s"]


def test_tautologies(tmp_path: Path) -> None:
    src = (
        "def test_t1():\n    assert True\n"
        "def test_t2():\n    x = 1\n    assert x == x\n"
        "def test_t3():\n    assert 2 == 2\n"
        "def test_t4(mocker):\n    m = mocker.MagicMock()\n    m()\n    assert m.called\n"
        "def test_ok():\n    x = 1\n    assert x == 2 - 1\n"
    )
    flags = _scan(tmp_path, src)
    for name in ("test_t1", "test_t2", "test_t3", "test_t4"):
        assert "tautology" in flags[name], name
    assert "tautology" not in flags["test_ok"]


def test_weak_only_assertions(tmp_path: Path) -> None:
    src = (
        "def test_w():\n    r = object()\n"
        "    assert r is not None\n    assert isinstance(r, object)\n"
        "def test_s():\n    r = 1\n    assert r is not None\n    assert r == 1\n"
    )
    flags = _scan(tmp_path, src)
    assert "weak_only" in flags["test_w"]
    assert "weak_only" not in flags["test_s"]


def test_long_and_mock_heavy(tmp_path: Path) -> None:
    body = "".join(f"    v{i} = {i}\n" for i in range(90))
    mocks = "".join(f"    mocker.patch('os.x{i}')\n" for i in range(10))
    src = (
        f"def test_long():\n{body}    assert v0 == 0\n"
        f"def test_mock(mocker):\n{mocks}    assert 1 == 1 + 0\n"
    )
    flags = _scan(tmp_path, src)
    assert "long" in flags["test_long"]
    assert "mock_heavy" in flags["test_mock"]


def test_duplicate_clusters_with_literal_variation(tmp_path: Path) -> None:
    template = (
        "def test_{n}():\n    a = len('{lit}')\n    b = a + {k}\n"
        "    result = sorted([b, a, {k}])\n    assert result[0] == {k} or result[1] == a\n"
        "    assert str(result) != '{lit}'\n"
    )
    src = template.format(n="one", lit="aa", k=1) + template.format(n="two", lit="bbb", k=2)
    src += "def test_other():\n    assert 1 + 1 == 2\n"
    (tmp_path / "test_x.py").write_text(src, encoding="utf-8")
    res = audit.scan(tmp_path, tmp_path)
    assert len(res["clusters"]) == 1
    cluster = res["clusters"][0]
    assert cluster["tier"] == "shape"
    assert cluster["parametrize_candidate"] is True
    assert cluster["size"] == 2


def test_class_methods_are_collected(tmp_path: Path) -> None:
    flags = _scan(tmp_path, "class TestK:\n    def test_m(self):\n        pass\n")
    assert "TestK.test_m" in flags
