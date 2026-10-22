"""Tests for the resumable setup step engine. Fully mocked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jailbee.macsetup.engine import (
    MacPaths,
    SetupContext,
    SetupError,
    SetupOptions,
    SetupState,
    ask,
    load_state,
    run_steps,
    save_state,
)


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)


class FakeStep:
    def __init__(self, id: str, fingerprint: str = "1", *, fail: SetupError | None = None) -> None:
        self.id = id
        self.title = f"Step {id}"
        self._fingerprint = fingerprint
        self._fail = fail
        self.runs = 0

    def fingerprint(self, ctx: SetupContext) -> str:
        return self._fingerprint

    def run(self, ctx: SetupContext) -> None:
        self.runs += 1
        if self._fail is not None:
            raise self._fail


def make_ctx(tmp_path: Path, *, interactive: bool = True, assume_yes: bool = False) -> SetupContext:
    paths = MacPaths(
        config_dir=tmp_path / "cfg",
        state_file=tmp_path / "cfg" / "setup-state.json",
        kernels_dir=tmp_path / "kernels",
        ssh_dir=tmp_path / "cfg" / "ssh",
    )
    return SetupContext(
        ac=None,  # type: ignore[arg-type]  # engine tests never touch Apple's CLI
        state=SetupState(options=SetupOptions(assume_yes=assume_yes), done={}, facts={}),
        paths=paths,
        interactive=interactive,
        confirm=lambda question: True,
        log=Recorder(),
    )


def test_up_to_date_step_is_skipped(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.state.done["a"] = "1"
    step = FakeStep("a", "1")
    assert run_steps(ctx, [step]) == 0
    assert step.runs == 0
    assert any("up to date" in line for line in ctx.log.lines)  # type: ignore[attr-defined]


def test_changed_fingerprint_reruns_step(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.state.done["a"] = "old"
    step = FakeStep("a", "new")
    assert run_steps(ctx, [step]) == 0
    assert step.runs == 1
    assert ctx.state.done["a"] == "new"


def test_state_saved_after_each_step_before_failure(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    first, second = FakeStep("a"), FakeStep("b", fail=SetupError("boom"))
    assert run_steps(ctx, [first, second]) == 1
    saved = load_state(ctx.paths.state_file)
    assert saved is not None and saved.done == {"a": "1"}


def test_failure_prints_remedy_and_rerun_hint(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    step = FakeStep("a", fail=SetupError("The kernel did not build", remedy="Free 20 GiB"))
    assert run_steps(ctx, [step]) == 1
    text = "\n".join(ctx.log.lines)  # type: ignore[attr-defined]
    assert "The kernel did not build" in text
    assert "Free 20 GiB" in text
    assert 'Re-run "jailbee mac setup" to continue.' in text


def test_keyboard_interrupt_returns_130_with_rerun_hint(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)

    class Interrupted(FakeStep):
        def run(self, ctx: SetupContext) -> None:
            raise KeyboardInterrupt

    assert run_steps(ctx, [Interrupted("a")]) == 130
    assert any("Re-run" in line for line in ctx.log.lines)  # type: ignore[attr-defined]


def test_ask_non_interactive_raises_with_command(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, interactive=False)
    with pytest.raises(SetupError) as excinfo:
        ask(
            ctx,
            "Update Apple's default kernel?",
            "container system kernel set --recommended --force",
        )
    assert excinfo.value.exit_code == 2
    assert "container system kernel set --recommended --force" in (excinfo.value.remedy or "")
    assert "--yes" in (excinfo.value.remedy or "")


def test_ask_assume_yes_skips_the_prompt(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, interactive=False, assume_yes=True)
    assert ask(ctx, "q", "cmd") is True


def test_ask_interactive_uses_confirm(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.confirm = lambda question: False
    assert ask(ctx, "q", "cmd") is False


def test_save_state_is_atomic_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "setup-state.json"
    state = SetupState(
        options=SetupOptions(mode="isolated", machine="m", cpus=3, memory="6G", gui=False),
        done={"a": "1"},
        facts={"image": "local/x:abc"},
    )
    save_state(path, state)
    assert [p.name for p in path.parent.iterdir()] == ["setup-state.json"]
    assert json.loads(path.read_text())["schema"] == 1
    loaded = load_state(path)
    assert loaded == state


def test_load_state_none_when_missing_or_unknown_schema(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": 99}))
    assert load_state(bad) is None


def test_fingerprint_is_recorded_after_the_step_ran(tmp_path: Path) -> None:
    """A step's input can change by running it (an image that now exists)."""

    class Builds(FakeStep):
        def __init__(self) -> None:
            super().__init__("a", "before")

        def run(self, ctx: SetupContext) -> None:
            super().run(ctx)
            self._fingerprint = "after"

    ctx = make_ctx(tmp_path)
    assert run_steps(ctx, [Builds()]) == 0
    assert ctx.state.done["a"] == "after"


def test_apple_container_errors_become_a_clean_failure_with_the_rerun_hint(tmp_path: Path) -> None:
    from jailbee.mac_container import AppleContainerError

    ctx = make_ctx(tmp_path)

    class Boom(FakeStep):
        def run(self, ctx: SetupContext) -> None:
            raise AppleContainerError("the build ran out of disk")

    assert run_steps(ctx, [Boom("a")]) == 1
    text = "\n".join(ctx.log.lines)  # type: ignore[attr-defined]
    assert "the build ran out of disk" in text and "Re-run" in text


def test_missing_tool_errors_become_a_clean_failure(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)

    class NoTool(FakeStep):
        def run(self, ctx: SetupContext) -> None:
            raise FileNotFoundError("ssh-keygen")

    assert run_steps(ctx, [NoTool("a")]) == 1
    assert any("ssh-keygen" in line for line in ctx.log.lines)  # type: ignore[attr-defined]
