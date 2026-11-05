"""Pure helpers for selecting and cleaning captured service logs."""

import re
from collections.abc import Sequence

_TERMINAL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\r")


def strip_terminal(text: str) -> str:
    return _TERMINAL.sub("", text)


def select_lines(
    lines: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
    *,
    limit: int,
    before: int,
    after: int,
) -> list[str]:
    matches = [i for i, line in enumerate(lines) if any(p.search(line) for p in patterns)][-limit:]
    ranges: list[tuple[int, int]] = []
    for index in matches:
        start, end = max(0, index - before), min(len(lines), index + after + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    result: list[str] = []
    for start, end in ranges:
        if result:
            result.append("--")
        result.extend(lines[start:end])
    return result
