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


def test_http_fetch_failure_is_none(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(litellm_quota.urllib.request, "urlopen", urlopen)

    assert litellm_quota.http_fetch("http://10.0.0.3:4100", "sk", "m") is None
