"""`jailbee litellm quota` inside a container, against a fake proxy reply."""

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from jailbee import litellm_quota

CODEX_HEADERS = {
    "content-type": "application/json",
    "llm_provider-x-codex-plan-type": "plus",
    "llm_provider-x-codex-active-limit": "premium",
    "llm_provider-x-codex-primary-used-percent": "97",
    "llm_provider-x-codex-primary-window-minutes": "10080",
    "llm_provider-x-codex-primary-reset-after-seconds": "388831",
    "llm_provider-x-codex-primary-reset-at": "1796079694",
    "llm_provider-x-codex-secondary-used-percent": "12",
    "llm_provider-x-codex-secondary-window-minutes": "300",
    "llm_provider-x-codex-secondary-reset-after-seconds": "3700",
    "llm_provider-x-codex-secondary-reset-at": "1795694564",
    "llm_provider-x-codex-credits-has-credits": "False",
    "llm_provider-x-codex-credits-unlimited": "False",
}


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = tmp_path / "key"
    key.write_text("sk-jb-secret\n")
    profile = {"base_url": "http://10.0.0.3:4100", "key_file": str(key)}
    path = tmp_path / "litellm.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_profile": "codex",
                "profiles": {
                    "codex": {
                        **profile,
                        "tiers": {
                            "fable": "jb.codex.most-capable",
                            "opus": "jb.codex.capable",
                            "haiku": "jb.codex.cheap",
                        },
                    },
                    "deep": {**profile, "tiers": {"opus": "jb.deep.capable"}},
                },
            }
        )
    )
    monkeypatch.setenv("JAILBEE_LITELLM_CONFIG", str(path))
    monkeypatch.delenv("JAILBEE_LITELLM_PROFILE", raising=False)
    return path


class FakeProxy:
    """Replies per model; a plain reply without quota headers otherwise."""

    def __init__(self, **replies: dict[str, str] | None) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, base_url: str, key: str, model: str) -> dict[str, str] | None:
        self.calls.append((base_url, key, model))
        return self.replies.get(model, {"content-type": "application/json"})

    @property
    def models(self) -> list[str]:
        return [model for _, _, model in self.calls]


def test_text_reads_the_cheapest_tier(config, capsys):
    proxy = FakeProxy(**{"jb.codex.cheap": CODEX_HEADERS})

    assert litellm_quota.quota(fetch=proxy) == 0

    out = capsys.readouterr().out
    assert out.splitlines() == [
        "plan: plus  active limit: premium  credits: none",
        "  primary: 97% used of 7d window, resets in 4d 12h 0m",
        "  secondary: 12% used of 5h window, resets in 0d 1h 1m",
    ]
    assert proxy.calls == [("http://10.0.0.3:4100", "sk-jb-secret", "jb.codex.cheap")]


def test_json(config, capsys):
    proxy = FakeProxy(**{"jb.codex.cheap": CODEX_HEADERS})

    assert litellm_quota.quota(fetch=proxy, as_json=True) == 0

    assert json.loads(capsys.readouterr().out) == {
        "profile": "codex",
        "model": "jb.codex.cheap",
        "plan": "plus",
        "active_limit": "premium",
        "primary": {
            "used_percent": 97,
            "window_minutes": 10080,
            "reset_after_seconds": 388831,
            "reset_at": 1796079694,
        },
        "secondary": {
            "used_percent": 12,
            "window_minutes": 300,
            "reset_after_seconds": 3700,
            "reset_at": 1795694564,
        },
        "credits": {"has_credits": False, "unlimited": False},
    }


def test_window_of_zero_minutes_is_absent(config, capsys):
    headers = {**CODEX_HEADERS, "llm_provider-x-codex-secondary-window-minutes": "0"}

    assert litellm_quota.quota(fetch=FakeProxy(**{"jb.codex.cheap": headers}), as_json=True) == 0

    assert json.loads(capsys.readouterr().out)["secondary"] is None


@pytest.mark.parametrize("value", ["garbled", "inf", "-inf", "NaN"])
def test_invalid_numbers_are_missing(config, capsys, value):
    headers = {
        **CODEX_HEADERS,
        "llm_provider-x-codex-primary-used-percent": value,
        "llm_provider-x-codex-primary-reset-after-seconds": value,
        "llm_provider-x-codex-secondary-window-minutes": value,
    }

    assert litellm_quota.quota(fetch=FakeProxy(**{"jb.codex.cheap": headers}), as_json=True) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["primary"]["used_percent"] is None
    assert result["primary"]["reset_after_seconds"] is None
    assert result["secondary"] is None


@pytest.mark.parametrize("window", ["primary", "secondary"])
def test_missing_used_percent_is_unknown_in_text(config, capsys, window):
    headers = {
        k: v for k, v in CODEX_HEADERS.items() if k != f"llm_provider-x-codex-{window}-used-percent"
    }

    assert litellm_quota.quota(fetch=FakeProxy(**{"jb.codex.cheap": headers})) == 0

    out = capsys.readouterr().out
    assert f"{window}: ?% used" in out
    assert "None" not in out


def test_reset_falls_back_to_reset_at(config, capsys):
    headers = {
        k: v for k, v in CODEX_HEADERS.items() if not k.endswith("primary-reset-after-seconds")
    }

    litellm_quota.quota(fetch=FakeProxy(**{"jb.codex.cheap": headers}), now=1796079694 - 90061)

    assert "  primary: 97% used of 7d window, resets in 1d 1h 1m" in capsys.readouterr().out


def test_falls_back_to_the_next_tier_without_headers(config, capsys):
    proxy = FakeProxy(**{"jb.codex.capable": CODEX_HEADERS, "jb.codex.cheap": None})

    assert litellm_quota.quota(fetch=proxy, as_json=True) == 0

    assert json.loads(capsys.readouterr().out)["model"] == "jb.codex.capable"
    assert proxy.models == ["jb.codex.cheap", "jb.codex.capable"]


@pytest.mark.parametrize("how", ["option", "env"])
def test_profile_from_option_or_environment(config, capsys, monkeypatch, how):
    proxy = FakeProxy(**{"jb.deep.capable": CODEX_HEADERS})
    if how == "env":
        monkeypatch.setenv("JAILBEE_LITELLM_PROFILE", "deep")
        rc = litellm_quota.quota(fetch=proxy)
    else:
        rc = litellm_quota.quota(fetch=proxy, profile="deep")

    assert rc == 0
    assert proxy.models == ["jb.deep.capable"]


def test_no_quota_in_any_reply_exits_1(config, capsys):
    proxy = FakeProxy()

    assert litellm_quota.quota(fetch=proxy) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no subscription quota in the replies of profile codex" in captured.err
    assert proxy.models == ["jb.codex.cheap", "jb.codex.capable", "jb.codex.most-capable"]


def test_unknown_profile_exits_2(config, capsys):
    assert litellm_quota.quota(fetch=FakeProxy(), profile="nope") == 2

    assert "unknown profile 'nope'; profiles: codex, deep" in capsys.readouterr().err


def test_missing_config_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("JAILBEE_LITELLM_CONFIG", str(tmp_path / "absent.json"))

    assert litellm_quota.quota(fetch=FakeProxy()) == 2

    assert "no LiteLLM proxy configured" in capsys.readouterr().err


@pytest.mark.parametrize(
    "replacement, field",
    [
        ([], "object"),
        ({"profiles": ["codex"]}, "profiles"),
        ({"profiles": None}, "profiles"),
        ({"default_profile": []}, "profile name"),
        ({"profiles": {"codex": []}}, "profile"),
        ({"base_url": None}, "base_url"),
        ({"base_url": []}, "base_url"),
        ({"base_url": ""}, "base_url"),
        ({"key_file": []}, "key_file"),
        ({"key_file": None}, "key_file"),
        ({"tiers": []}, "tiers"),
        ({"tiers": None}, "tiers"),
        ({"tiers": {"haiku": ["sk-jb-secret"]}}, "tier models"),
    ],
)
def test_wrong_shape_config_exits_2(config, capsys, replacement, field):
    data = json.loads(config.read_text())
    if isinstance(replacement, dict):
        if set(replacement) & {"profiles", "default_profile"}:
            data.update(replacement)
        else:
            data["profiles"]["codex"].update(replacement)
    else:
        data = replacement
    config.write_text(json.dumps(data))
    proxy = FakeProxy()

    assert litellm_quota.quota(fetch=proxy) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid LiteLLM config" in captured.err
    assert field in captured.err
    assert "sk-jb-secret" not in captured.err
    assert not proxy.calls


def test_missing_base_url_exits_2(config, capsys):
    data = json.loads(config.read_text())
    del data["profiles"]["codex"]["base_url"]
    config.write_text(json.dumps(data))

    assert litellm_quota.quota(fetch=FakeProxy()) == 2

    assert "base_url" in capsys.readouterr().err


def test_invalid_proxy_url_exits_2_without_exposing_request(config, capsys):
    data = json.loads(config.read_text())
    data["profiles"]["codex"]["base_url"] = "sk-jb-secret"
    config.write_text(json.dumps(data))

    assert litellm_quota.quota() == 2

    captured = capsys.readouterr()
    assert "invalid proxy request configuration" in captured.err
    assert "sk-jb-secret" not in captured.err


class _Reply(io.BytesIO):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__(b"{}")
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name] = value


def test_http_fetch_posts_a_one_token_completion(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        return _Reply({"LLM_Provider-X-Codex-Plan-Type": "plus"})

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    headers = litellm_quota.http_fetch("http://10.0.0.3:4100", "sk", "jb.codex.cheap")

    assert headers == {"llm_provider-x-codex-plan-type": "plus"}
    assert seen == {
        "url": "http://10.0.0.3:4100/v1/chat/completions",
        "auth": "Bearer sk",
        "body": {
            "model": "jb.codex.cheap",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        },
    }


@pytest.mark.parametrize("as_json", [False, True])
def test_rate_limit_reply_still_reports_quota(config, monkeypatch, capsys, as_json):
    calls = []
    body = io.BytesIO(b"sk-jb-secret")

    def urlopen(request, timeout):
        calls.append(request)
        headers = Message()
        for name, value in CODEX_HEADERS.items():
            headers[name.upper()] = value
        raise urllib.error.HTTPError(request.full_url, 429, "sk-jb-secret", headers, body)

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    assert litellm_quota.quota(as_json=as_json) == 0

    captured = capsys.readouterr()
    if as_json:
        assert json.loads(captured.out)["primary"]["used_percent"] == 97
    else:
        assert "primary: 97% used of 7d window" in captured.out
    assert captured.err == ""
    assert "sk-jb-secret" not in captured.out
    assert len(calls) == 1
    assert body.closed


def test_last_http_failure_survives_later_headerless_success(config, monkeypatch, capsys):
    replies = iter([401, 500, None])

    def urlopen(request, timeout):
        status = next(replies)
        if status is None:
            return _Reply({})
        raise urllib.error.HTTPError(request.full_url, status, "secret", Message(), None)

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    assert litellm_quota.quota() == 4

    assert "HTTP 500" in capsys.readouterr().err


def test_http_failure_can_fall_back_to_a_quota_route(config, monkeypatch, capsys):
    replies = iter([401, None])

    def urlopen(request, timeout):
        if next(replies) == 401:
            raise urllib.error.HTTPError(request.full_url, 401, "secret", Message(), None)
        return _Reply(CODEX_HEADERS)

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    assert litellm_quota.quota(as_json=True) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["model"] == "jb.codex.capable"
    assert captured.err == ""


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_failure_is_reported_instead_of_no_quota(config, monkeypatch, capsys, status):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, status, "sk-jb-secret", Message(), io.BytesIO(b"sk-jb-secret")
        )

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    assert litellm_quota.quota() == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"HTTP {status}" in captured.err
    assert "no subscription quota" not in captured.err
    assert "sk-jb-secret" not in captured.err


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("sk-jb-secret refused"), ConnectionRefusedError("sk-jb-secret"),
     TimeoutError("sk-jb-secret")],
)
def test_connection_failure_stops_after_first_request(config, monkeypatch, capsys, error):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        raise error

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    assert litellm_quota.quota() == 3

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "proxy unreachable" in captured.err
    assert "no subscription quota" not in captured.err
    assert "sk-jb-secret" not in captured.err
    assert len(calls) == 1
