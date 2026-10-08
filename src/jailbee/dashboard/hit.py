"""Click targets in a rendered dashboard frame.

A frame tags each clickable part with a Rich ``Style`` meta entry under
:data:`HIT_KEY`; a frontend reads it back from the style under the pointer.
Plain data, no Textual: the renderers that tag live in the shared core.

Not ``@click``: Textual restyles every ``@click`` segment with its link
colours, which would repaint the whole table, so the dashboard draws its own
hover (:data:`HOVER_STYLE`) instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

from rich.segment import Segment
from rich.style import Style

HIT_KEY = "@jb.hit"

HitKind = Literal[
    "row",
    "repo",
    "fold",
    "scroll",
    "suggestion",
]
_KINDS: frozenset[str] = frozenset(get_args(HitKind))
TABLE_HIT_KINDS: frozenset[HitKind] = frozenset({"row", "repo", "fold", "scroll"})

# A dim background, distinct from the cursor's bold magenta foreground.
HOVER_STYLE = Style(bgcolor="grey23")


@dataclass(frozen=True)
class Hit:
    """One click target: what it is and which one."""

    kind: HitKind
    args: tuple[str | int, ...] = ()

    @classmethod
    def of(cls, meta: Mapping[str, object]) -> Hit | None:
        """The target tagged in ``meta``, or None for an untagged cell."""
        value = meta.get(HIT_KEY)
        if not isinstance(value, tuple) or not value or value[0] not in _KINDS:
            return None
        args = value[1:]
        if not all(isinstance(arg, (str, int)) for arg in args):
            return None
        return cls(cast("HitKind", value[0]), args)

    def meta_value(self) -> tuple[str | int, ...]:
        return (self.kind, *self.args)


def hover_segments(segments: Iterable[Segment], hover: Hit | None) -> list[Segment]:
    """Apply the hover style to segments tagged for ``hover``."""
    rendered = list(segments)
    if hover is None:
        return rendered
    target = hover.meta_value()
    return [
        Segment(segment.text, segment.style + HOVER_STYLE, segment.control)
        if segment.style is not None and segment.style.meta.get(HIT_KEY) == target
        else segment
        for segment in rendered
    ]


def hit_style(kind: HitKind, *args: str | int) -> Style:
    """A style carrying only the tag; combine it with the segment's own style."""
    return Style(meta={HIT_KEY: (kind, *args)})


def hit_markup(markup: str, kind: HitKind, *args: str | int) -> str:
    """``markup`` wrapped in the tag, for renderers that build Rich markup lines.

    Only for args whose ``repr`` is markup-safe (ints and fixed identifiers);
    names go through :func:`hit_style` on a ``Text``.
    """
    return f"[{HIT_KEY}={(kind, *args)!r}]{markup}[/]"
