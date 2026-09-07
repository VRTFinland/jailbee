"""Tests for the apps: config mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jailbee.config.models_apps import AppEntry


def test_command_is_required():
    with pytest.raises(ValidationError):
        AppEntry.model_validate({})


def test_string_command_is_split_into_argv():
    # A one-line `command: /opt/x/x --flag` is what people write; splitting
    # it here means every consumer sees a list and nobody re-parses.
    e = AppEntry.model_validate({"command": "/opt/x/x --flag"})
    assert e.command == ["/opt/x/x", "--flag"]


def test_cwd_defaults_to_repo():
    assert AppEntry.model_validate({"command": "x"}).cwd == "repo"


def test_cwd_rejects_a_relative_path():
    with pytest.raises(ValidationError):
        AppEntry.model_validate({"command": "x", "cwd": "sub/dir"})


def test_cwd_accepts_an_absolute_container_path():
    e = AppEntry.model_validate({"command": "x", "cwd": "/srv/thing"})
    assert e.cwd == "/srv/thing"


def test_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        AppEntry.model_validate({"command": "x", "detach": True})


def test_command_empty_string_is_rejected():
    # Empty string splits to empty list via _split_command, then _command_not_empty rejects it.
    with pytest.raises(ValidationError):
        AppEntry.model_validate({"command": ""})


def test_command_empty_list_is_rejected():
    # Empty list reaches _command_not_empty directly without split.
    with pytest.raises(ValidationError):
        AppEntry.model_validate({"command": []})


def test_top_level_app_colliding_with_a_real_command_is_an_error(tmp_path):
    import typer.main

    from jailbee.cli import app as cli_app
    from tests.conftest import make_cfg

    # Assert against the live Typer command list, not a hardcoded copy:
    # a command added later must not silently stop colliding.
    existing = sorted(typer.main.get_command(cli_app).commands)
    victim = existing[0]
    cfg = make_cfg(tmp_path, apps={victim: {"command": "/bin/true", "top_level": True}})
    assert any(victim in i for i in cfg.validate_runtime())


def test_illegal_app_name_is_an_error(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"My App": {"command": "/bin/true"}})
    assert any("My App" in i for i in cfg.validate_runtime())
