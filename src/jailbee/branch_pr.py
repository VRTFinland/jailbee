"""Per-head PR bindings stored on a container."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.incus import Incus

BRANCH_PRS_LABEL = "user.jailbee.branch_prs"


def default_head(source: str) -> str:
    if source.startswith("refs/jailbee/from/"):
        parts = source.split("/", 4)
        if len(parts) == 5:
            return parts[4]
    if source.startswith("refs/heads/"):
        return source.removeprefix("refs/heads/")
    return source.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class BranchPr:
    head: str
    pr: int
    base: str
    source: str
    source_sha: str
    author: bool = True
    adopted: bool = False


class BranchPrMap:
    def __init__(self, incus: Incus, full: str) -> None:
        self._incus = incus
        self._full = full

    def read(self) -> dict[str, BranchPr]:
        raw = self._incus.config_get_checked(self._full, BRANCH_PRS_LABEL)
        try:
            data = json.loads(raw or "{}")
        except (ValueError, TypeError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, BranchPr] = {}
        for head, entry in data.items():
            if not isinstance(head, str) or not head or not isinstance(entry, dict):
                continue
            if entry.get("head") != head:
                continue
            number = entry.get("pr")
            if type(number) is not int or number <= 0:
                continue
            if any(not isinstance(entry.get(k), str) or not entry[k] for k in ("base", "source", "source_sha")):
                continue
            author, adopted = entry.get("author", True), entry.get("adopted", False)
            if not isinstance(author, bool) or not isinstance(adopted, bool):
                continue
            result[head] = BranchPr(head, number, entry["base"], entry["source"], entry["source_sha"], author, adopted)
        return result

    def get(self, head: str) -> BranchPr | None:
        return self.read().get(head)

    def put(self, entry: BranchPr) -> None:
        entries = self.read()
        entries[entry.head] = entry
        self._write(entries)

    def drop(self, head: str) -> bool:
        entries = self.read()
        if head not in entries:
            return False
        del entries[head]
        if entries:
            self._write(entries)
        else:
            self._incus.config_unset_checked(self._full, BRANCH_PRS_LABEL)
        return True

    def numbers(self) -> frozenset[int]:
        return frozenset(entry.pr for entry in self.read().values())

    def _write(self, entries: dict[str, BranchPr]) -> None:
        self._incus.config_set(
            self._full, BRANCH_PRS_LABEL,
            json.dumps({head: asdict(entry) for head, entry in entries.items()}, sort_keys=True),
        )
