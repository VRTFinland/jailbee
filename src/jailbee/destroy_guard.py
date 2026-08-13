"""Decide whether destroying a container would discard unrecoverable work.

Pure assessment: it reads a ``ContainerInfo`` that already carries a
``GitStatus`` (so no ``incus exec`` happens here) plus one local
``git cat-file -e`` on the host. That makes it cheap enough to call from
the Qt GUI thread before spawning a detached destroy.

The guard adds a confirmation; it never refuses. And it never reports
"safe" from missing data — an unmeasured field is not a clean field. That
includes the case where every field is unmeasured at once (a fully-failed
probe on a *reachable* container): that state gets its own reason rather
than being folded into "nothing at risk". See ``assess``'s docstring for
the full ``None``-return contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.git import has_commit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.config import Config
    from jailbee.git_status import GitStatus, SubmoduleChange
    from jailbee.lifecycle import ContainerInfo

# Values of a rendered git field that mean "not measured" rather than a
# real measurement. Treating these as changes would fire the warning on
# every stopped container.
_UNMEASURED = ("clean", "—", "?", "")

# User-facing text for a probed-but-unreadable container (Finding 2): every
# field came back unmeasured, so jailbee observed nothing — not "clean".
_NOTHING_MEASURABLE_REASON = "could not inspect the container"


def status_is_unknown(ci: ContainerInfo) -> bool:
    """True when ``ci``'s git state is unmeasured *and* that matters.

    ``git_status is None`` covers two very different situations. A stopped
    or unprobed clone-mode container really is unknown: its work lives only
    inside the container, so callers must say so rather than imply safety.
    A **mount-mode** container is not unknown but *not applicable* — its
    working tree **is** the host's directory, bind-mounted in, and it
    survives the destroy untouched. `lifecycle.list_containers` and the
    CLI's single-name path both skip probing mount mode for exactly that
    reason (`jailbee ls` renders those git columns as ``—``), so folding them
    into an "uncommitted work may be lost" note would be the one case
    where the warning is provably false.

    One implementation shared by the CLI (`cli._warn_before_destroy`) and
    the Qt dialog (`qtui.app.AppController._confirm_destroy`).
    """
    return ci.git_status is None and ci.mode != "mount"


def unknown_status_warning(names: Sequence[str]) -> str:
    """The single wording for "jailbee could not measure these containers".

    Shared verbatim by the CLI's pre-destroy note and the Qt confirm
    dialog: the same container must not be described two different ways
    depending on which front-end asked. The CLI can pass several names at
    once (`--all`, the picker) as well as one, so the pronoun agrees with
    ``names``'s length rather than always reading "it".
    """
    pronoun = "it" if len(names) == 1 else "them"
    return (
        f"git status unknown for: {', '.join(names)} — jailbee could not measure "
        f"{pronoun}, so a destroy may discard uncommitted work"
    )


def _submodule_reason(sub: SubmoduleChange) -> str:
    """One reason line for a changed submodule.

    ``git_status._parse_submodules`` admits an entry when *any* of the
    committed delta, the commit count, the working-tree delta, or an
    added/removed gitlink is set. Printing only the committed numbers
    therefore rendered a dirty-only submodule — or an added/removed one —
    as ``+0 -0``: figures that say "nothing changed" while being the sole
    reason for the prompt. Each signal gets its own phrase instead.
    """
    parts: list[str] = []
    if sub.status == "new":
        parts.append("added")
    elif sub.status == "removed":
        parts.append("removed")
    if sub.ahead_ins or sub.ahead_del:
        parts.append(f"committed +{sub.ahead_ins} -{sub.ahead_del}")
    elif sub.ahead_commits:
        parts.append(f"{sub.ahead_commits} commits")
    if sub.wt_ins or sub.wt_del:
        parts.append(f"uncommitted +{sub.wt_ins} -{sub.wt_del}")
    if not parts:
        # Defensive: an entry admitted by a signal none of the branches
        # above can name (today, impossible). Never render bare zeros.
        parts.append("changed")
    return f"submodule {sub.path} ({', '.join(parts)})"


def _nothing_measurable(status: GitStatus) -> bool:
    """True when the probe returned no usable signal at all.

    Distinct from any single unknown field (which the caller already
    treats as unknown-not-clean on a per-condition basis): this is the
    *compound* case where the working tree, commit count, head sha, remote
    containment, and submodules are all unmeasured together — exactly what
    a fully-failed probe (`IncusError`, timeout, malformed output) leaves
    behind on a still-``Running`` container. Left unnamed, that state reads
    identically to a verified-clean container.
    """
    return (
        status.wt in _UNMEASURED
        and not status.ahead_count.isdigit()
        and not status.head_sha
        and status.remote_contained is None
        and not status.submodules
    )


@dataclass(frozen=True)
class RiskSummary:
    """What a destroy would discard, and how to say it in one line."""

    container: str
    reasons: tuple[str, ...]

    @property
    def line(self) -> str:
        return f"{self.container}: " + " · ".join(self.reasons)


def assess(cfg: Config, ci: ContainerInfo) -> RiskSummary | None:
    """Summarise what destroying ``ci`` would lose, or None when nothing would.

    ``None`` also comes back when ``ci.git_status`` is None — the container
    was never probed (stopped containers aren't) or the probe was skipped,
    so nothing is *knowable*. Callers must not read that as safety: they
    show their normal confirmation plus a note that git status is unknown.

    That contract does **not** extend to a container that *was* probed but
    came back with no usable signal at all (every field unmeasured — a
    fully-failed `incus exec`). That is knowable, and what's known is "jailbee
    could not look" — so it comes back as a one-reason ``RiskSummary``,
    never as silent ``None``.
    """
    status = ci.git_status
    if status is None:
        return None

    if _nothing_measurable(status):
        return RiskSummary(container=ci.display_name, reasons=(_NOTHING_MEASURABLE_REASON,))

    reasons: list[str] = []

    if status.wt not in _UNMEASURED:
        reasons.append(f"working tree {status.wt}")

    reasons.extend(_submodule_reason(sub) for sub in status.submodules)

    # Commits are only at risk when they exist nowhere else: not on the
    # host (any earlier `jailbee git pull` put them there) and not behind a
    # remote-tracking ref (`jailbee git push`, or a plain push from inside).
    # `remote_contained is None` is unknown, which counts as at-risk.
    #
    # An unmeasured `ahead_count` ("?" — the probe could not resolve the
    # container's base, which is the PR-review case `?` exists for) must
    # not turn into silence either: the count feeds only the *message*,
    # while the at-risk question is answered by `head_sha` alone. That is
    # safe against over-warning because `has_commit` tests an object, not a
    # ref — a container parked on its base tip still answers "on the host",
    # and one parked on a commit any remote-tracking ref reaches answers
    # `remote_contained`. Neither warns.
    ahead_known = status.ahead_count.isdigit()
    if (ahead_known and int(status.ahead_count) > 0) or (not ahead_known and status.head_sha):
        on_host = bool(status.head_sha) and has_commit(cfg.repo_root, status.head_sha)
        if not on_host and status.remote_contained is not True:
            reasons.append(
                f"{status.ahead_count} commits not on the host"
                if ahead_known
                else "commits not on the host (count unknown)"
            )

    if not reasons:
        return None
    return RiskSummary(container=ci.display_name, reasons=tuple(reasons))
