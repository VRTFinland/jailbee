from jailbee.qtui import terminal as t


def _which_only(*present):
    allowed = set(present)
    return lambda name: f"/usr/bin/{name}" if name in allowed else None


def test_detects_first_available_in_priority_order():
    spec = t.detect_terminal(env={}, which=_which_only("xterm", "konsole"))
    # konsole outranks xterm in DETECT_ORDER
    assert spec is not None
    assert spec.binary == "konsole"
    assert spec.run_args == ["-e"]


def test_gnome_terminal_uses_double_dash():
    spec = t.detect_terminal(env={}, which=_which_only("gnome-terminal"))
    assert spec is not None
    assert spec.binary == "gnome-terminal"
    assert spec.run_args == ["--"]


def test_kitty_takes_bare_command():
    spec = t.detect_terminal(env={}, which=_which_only("kitty"))
    assert spec is not None and spec.run_args == []


def test_env_override_takes_priority():
    spec = t.detect_terminal(env={"JAILBEE_TERMINAL": "foot"}, which=_which_only("foot", "xterm"))
    assert spec is not None and spec.binary == "foot"


def test_unknown_override_defaults_to_dash_e():
    spec = t.detect_terminal(env={"JAILBEE_TERMINAL": "myterm"}, which=_which_only("myterm"))
    assert spec is not None and spec.binary == "myterm" and spec.run_args == ["-e"]


def test_returns_none_when_nothing_found():
    assert t.detect_terminal(env={}, which=lambda name: None) is None


def test_build_terminal_command_composes_argv():
    spec = t.TerminalSpec(binary="gnome-terminal", run_args=["--"])
    argv = t.build_terminal_command(spec, ["gie", "shell", "p-foo", "--config", "/x"])
    assert argv == ["gnome-terminal", "--", "gie", "shell", "p-foo", "--config", "/x"]
