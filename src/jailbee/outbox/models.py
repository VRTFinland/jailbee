"""Immutable outbox inputs and presentation values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jailbee.outbox_io import ContainerIdentity

Kind = Literal["pr", "issue"]
State = Literal["pending", "partial", "applied", "uncertain", "invalid", "awaiting-pr"]


class OutboxError(ValueError):
    """Invalid input or blocked local operation."""


class OutboxChanged(OutboxError):  # noqa: N818 - public refresh exception specified by the service API.
    """Refresh required before acting on a stale view."""


class OutboxExecutionError(RuntimeError):
    """An outbox operation could not be executed."""


@dataclass(frozen=True)
class ProposalId:
    kind: Kind
    name: str

    def __post_init__(self) -> None:
        if self.kind not in ("pr", "issue") or (
            not self.name.endswith(".json")
            or self.name.endswith(".progress.json")
            or any(c in self.name for c in ("/", "\\", "\0"))
            or self.name in (".json", "..json")
            or any(ord(c) < 32 or ord(c) == 127 for c in self.name)
        ):
            raise OutboxError("expected pr/<manifest>.json or issue/<manifest>.json")

    @classmethod
    def parse(cls, value: str) -> ProposalId:
        kind, separator, name = value.partition("/")
        if not separator or kind not in ("pr", "issue"):
            raise OutboxError("expected pr/<manifest>.json or issue/<manifest>.json")
        return cls("pr" if kind == "pr" else "issue", name)

    def __str__(self) -> str:
        return f"{self.kind}/{self.name}"


@dataclass(frozen=True)
class CommentView:
    index: int
    label: str
    text: str


@dataclass(frozen=True)
class ActionView:
    """One action of a proposal. `body` is Markdown only when `markdown` says so.

    A title is kept apart from the body because GitHub shows it as plain text:
    laying it out as Markdown would turn a `#` or `*` in it into styling.
    """

    index: int
    kind: str
    repo: str
    target: str
    title: str | None
    body: str | None
    markdown: bool
    state: Literal["pending", "applied", "uncertain"]
    receipt: str | None
    comments: tuple[CommentView, ...]

    @property
    def text(self) -> str:
        """Title and body as one plain text, for JSON and the plain-text views."""
        if self.title is None:
            return self.body or ""
        if self.body is None:
            return self.title
        return f"{self.title}\n\n{self.body}"


@dataclass(frozen=True)
class StoreSnapshot:
    kind: Kind
    files: tuple[tuple[str, str], ...]
    rejected: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, str]:
        """Return content only; retain this snapshot for strict progress interpretation."""
        return dict(self.files)


@dataclass(frozen=True)
class ProposalView:
    id: ProposalId
    revision: str
    raw_text: str
    actions: tuple[ActionView, ...]
    state: State
    error: str | None
    edit_block: str | None


@dataclass(frozen=True)
class ContainerView:
    identity: ContainerIdentity | None
    name: str
    available: bool
    error: str | None
    stores: tuple[StoreSnapshot, ...]
    proposals: tuple[ProposalView, ...]
