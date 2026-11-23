"""Telling a Claude session stuck on an API error from one that recovered.

Fixture records are shaped after a real transcript (2026-11-23): Claude Code
retries a failing request silently, then writes one synthetic assistant
record with `isApiErrorMessage: true` and gives up. Pure: `parse_tail` over a
byte buffer.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime

from jailbee.accounts.adapters import claude_activity as ca

NOW = datetime(2026, 11, 23, 20, 0, tzinfo=UTC).timestamp()

QUOTA_TEXT = (
    "API Error: Request rejected (429) · litellm.RateLimitError: RateLimitError: "
    'ChatgptException - {"error":{"type":"usage_limit_reached","message":"The usage '
    'limit has been reached","plan_type":"plus","resets_at":1795474751,'
    '"eligible_promo":null,"limit_window_minutes":10080,"resets_in_seconds":11822}}'
    "\n\nLiteLLM: model group 'jb.codex.capable' failed with the error above. "
    "No fallback was attempted."
)


def _real(stamp: str, text: str = "working on it") -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]},
    }


def _error(
    stamp: str = "2026-11-23T19:42:12.085Z",
    *,
    text: str = QUOTA_TEXT,
    error: object = "rate_limit",
    status: object = 429,
    error_type: object = "rate_limit_error",
    uuid: str = "e1",
) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "isApiErrorMessage": True,
        "error": error,
        "apiErrorStatus": status,
        "apiErrorType": error_type,
        "apiErrorIsTransient": False,
        "uuid": uuid,
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _parse(*records: dict[str, object]) -> ca.TailActivity:
    raw = "\n".join(json.dumps(r) for r in records).encode()
    return ca.parse_tail(raw, now=NOW)


def test_a_quota_error_with_nothing_after_it_is_an_open_error() -> None:
    parsed = _parse(_real("2026-11-23T19:37:59Z"), _error())

    error = parsed.api_error
    assert error is not None
    assert error.kind == "quota"
    assert error.status == 429
    assert error.at == datetime(2026, 11, 23, 19, 42, 12, 85000, tzinfo=UTC)
    assert error.resets_at == datetime.fromtimestamp(1795474751, UTC)
    assert "usage limit" in error.detail.lower()
    assert "\n" not in error.detail
    assert error.key == "2026-11-23T19:42:12.085000+00:00|e1"


def test_the_error_record_is_not_progress() -> None:
    parsed = _parse(_real("2026-11-23T19:37:59Z"), _error())

    assert parsed.last_event_at == datetime(2026, 11, 23, 19, 37, 59, tzinfo=UTC)


def test_error_kinds_follow_status_type_and_text() -> None:
    cases = [
        (_error(text="API Error: Request rejected (429) · slow down"), "rate_limit"),
        (
            _error(
                text="API Error: 401 invalid x-api-key",
                error="authentication_failed",
                status=401,
                error_type="authentication_error",
            ),
            "auth",
        ),
        (
            _error(text="API Error: 403", error="unknown", status=403, error_type=None),
            "auth",
        ),
        (
            _error(
                text="API Error: Overloaded",
                error="unknown",
                status=529,
                error_type="overloaded_error",
            ),
            "overloaded",
        ),
        (
            _error(text="API Error: something broke", error=None, status=500, error_type=None),
            "other",
        ),
        (
            _error(text='{"error":{"type":"insufficient_quota"}}', error=None, status=429),
            "quota",
        ),
    ]
    for record, kind in cases:
        error = _parse(record).api_error
        assert error is not None, kind
        assert error.kind == kind, record
        assert error.resets_at is None


def test_a_rate_limit_detail_drops_the_prefix_and_body() -> None:
    error = _parse(_error(text='API Error: Request rejected (429) · {"secret": "body"}')).api_error

    assert error is not None
    assert error.detail == "Request rejected (429)"


def test_the_error_stays_open_while_only_non_progress_follows() -> None:
    followers = [
        {"type": "last-prompt", "lastPrompt": "x"},
        {"type": "cost-state"},
        {
            "type": "user",
            "timestamp": "2026-11-23T19:42:13Z",
            "origin": {"kind": "task-notification"},
            "message": {"content": "<task-notification>done</task-notification>"},
        },
        {"type": "api-request", "querySource": "repl_main_thread"},
        {
            "type": "assistant",
            "timestamp": "2026-11-23T19:48:40Z",
            "isApiErrorMessage": False,
            "madeBy": "other",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "No response requested."}],
            },
        },
    ]
    parsed = _parse(_real("2026-11-23T19:37:59Z"), _error(), *followers)

    assert parsed.api_error is not None
    assert parsed.api_error.kind == "quota"


def test_a_real_reply_after_the_error_means_recovered() -> None:
    parsed = _parse(_error(), _real("2026-11-23T19:50:00Z", "back again"))

    assert parsed.api_error is None
    assert parsed.last_event_at == datetime(2026, 11, 23, 19, 50, tzinfo=UTC)


def test_the_newest_error_wins_over_an_older_one() -> None:
    parsed = _parse(
        _error("2026-11-23T19:00:00Z", uuid="old"),
        _real("2026-11-23T19:10:00Z"),
        _error(
            "2026-11-23T19:42:00Z",
            text="API Error: Overloaded",
            error="unknown",
            status=529,
            error_type="overloaded_error",
            uuid="new",
        ),
    )

    assert parsed.api_error is not None
    assert parsed.api_error.kind == "overloaded"
    assert parsed.api_error.key.endswith("|new")


def test_hostile_error_records_are_bounded_and_never_raise() -> None:
    hostile = [
        _error(text="API Error: \x1b[31mred\x07\x00 " + "x" * 200_000),
        _error(text='API Error: usage limit {"resets_at": 99999999999}'),
        _error(text='API Error: usage limit {"resets_at": 1}'),
        _error(text="API Error: usage limit {not json", status="429", error=["x"]),
        _error(text="", status=None, error=None, error_type=None, uuid="\n" * 500),
        {"type": "assistant", "isApiErrorMessage": True, "message": "nope"},
        {"type": "assistant", "isApiErrorMessage": True, "timestamp": 5},
    ]
    for record in hostile:
        error = _parse(record).api_error
        assert error is not None
        assert len(error.detail) <= 160
        assert error.detail.isprintable()
        assert error.resets_at is None
        assert len(error.key) <= 100


def test_read_activity_carries_the_error(tmp_path: Path) -> None:
    from jailbee.accounts.models import ActivityPaths

    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (_real("2026-11-23T19:37:59Z"), _error())))

    activity = ca.read_activity(ActivityPaths(path, tmp_path / "subagents"), now=NOW)

    assert activity is not None
    assert activity.api_error is not None
    assert activity.api_error.kind == "quota"
