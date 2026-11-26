"""Keep provisioned shell option arities aligned with the Python launchers."""

import importlib.resources
import re

from jailbee.claude_options import VALUE_OPTIONS


def test_shell_settings_option_tables_match_python() -> None:
    text = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    for start, end in (("claude() (", "\n)"), ("add_settings() {", "\n}")):
        scanner = text.split(start, 1)[1].split(end, 1)[0]
        case = scanner.split("--settings|--settings=*)", 1)[1]
        options = case.split(")", 1)[0].split(";;", 1)[1]
        assert set(re.findall(r"--?[A-Za-z][A-Za-z-]*", options)) == VALUE_OPTIONS - {"--settings"}
