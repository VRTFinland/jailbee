import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from jailbee.services.local_proc import ProbeResult
from jailbee.services.probe_cache import ProbeCache


OK = ProbeResult(0, "ok", "", False)
FAIL = ProbeResult(1, "", "bad", False)


def test_hit_expiry_and_launch_isolation(tmp_path):
    clock = [10.0]
    cache = ProbeCache(tmp_path / "probes.json", ttl=5, now=lambda: clock[0])
    cache.put("api", "health", "launch1", OK)
    assert cache.get("api", "health", "launch1") == OK
    assert cache.get("api", "status", "launch1") is None
    assert cache.get("api", "health", "launch2") is None
    clock[0] = 16
    assert cache.get("api", "health", "launch1") is None


def test_first_failure_survives_expiry_and_unrelated_prune(tmp_path):
    clock = [10.0]
    path = tmp_path / "probes.json"
    cache = ProbeCache(path, ttl=5, now=lambda: clock[0])
    cache.put("api", "health", "one", FAIL)
    clock[0] = 20
    cache.put("other", "health", "one", OK)
    assert cache.get("api", "health", "one") is None
    cache.put("api", "health", "one", FAIL)
    assert cache.first_failure("api", "health", "one") == 10
    cache.put("api", "health", "one", OK)
    assert cache.first_failure("api", "health", "one") is None
    clock[0] = 21
    cache.put("api", "health", "one", FAIL)
    assert cache.first_failure("api", "health", "one") == 21


@pytest.mark.parametrize("content", ["{", "[]", '{"entries": {"x": 1}}', '�'])
def test_corrupt_cache_is_a_miss_and_can_be_replaced(tmp_path, content):
    path = tmp_path / "probes.json"
    path.write_text(content)
    cache = ProbeCache(path)
    assert cache.get("api", "health", "one") is None
    assert cache.first_failure("api", "health", "one") is None
    cache.put("api", "health", "one", OK)
    assert cache.get("api", "health", "one") == OK


def test_concurrent_instances_do_not_lose_writes(tmp_path):
    path = tmp_path / "probes.json"
    def write(i):
        ProbeCache(path).put(str(i), "health", "one", OK)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(write, range(100)))
    cache = ProbeCache(path)
    assert all(cache.get(str(i), "health", "one") == OK for i in range(100))
    json.loads(path.read_text())


def test_malformed_fields_are_misses(tmp_path):
    path = tmp_path / "probes.json"
    key = json.dumps(["api", "health", "one"])
    for entry in [
        {"timestamp": True, "code": 0, "stdout": "", "stderr": "", "timed_out": False},
        {"timestamp": 1, "code": True, "stdout": "", "stderr": "", "timed_out": False},
        {"timestamp": 10**400, "first_failed_at": 10**400},
    ]:
        path.write_text(json.dumps({"entries": {key: entry}}))
        cache = ProbeCache(path, now=lambda: 1)
        assert cache.get("api", "health", "one") is None
        assert cache.first_failure("api", "health", "one") is None
        cache.put("other", "health", "one", OK)


def test_timeout_counts_as_failure(tmp_path):
    cache = ProbeCache(tmp_path / "probes.json", now=lambda: 42.0)
    cache.put("api", "health", "one", ProbeResult(0, "", "", True))
    assert cache.first_failure("api", "health", "one") == 42
