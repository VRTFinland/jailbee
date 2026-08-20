"""Build the base golden Incus image (alias derived from container_prefix)."""

from __future__ import annotations

import base64
import importlib.resources
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from jailbee.config import Config
from jailbee.incus import Incus, IncusError
from jailbee.init_command import LOOSE_BRIDGE
from jailbee.tui import info, status_with_elapsed, success, warn


def _java_apt_package(java_id: str) -> str:
    """Map config java identifier to apt package name.

    e.g. ``amazon-corretto-17`` → ``java-17-amazon-corretto-jdk``.
    Pass through if user already specified an apt package name.
    """
    if java_id.startswith("amazon-corretto-"):
        version = java_id.removeprefix("amazon-corretto-")
        return f"java-{version}-amazon-corretto-jdk"
    return java_id


def _logical_name(filename: str) -> str:
    """'30-nodejs.sh' -> 'nodejs'; '90-registry-mirror-ca.sh' -> 'registry-mirror-ca'."""
    stem = filename[:-3] if filename.endswith(".sh") else filename
    m = re.match(r"^\d+-(.+)$", stem)
    return m.group(1) if m else stem


def resolve_available(available_dir: Path, enabled: list[str]) -> tuple[list[Path], list[str]]:
    """Match enable_snippets names against the bundled available library.

    A name matches by full filename ('30-nodejs' / '30-nodejs.sh') or by
    logical name ('nodejs'). Returns (matched paths, unknown names).
    """
    available: dict[str, Path] = {}
    if available_dir.is_dir():
        for p in sorted(available_dir.glob("*.sh")):
            available[p.name] = p
    matched: dict[str, Path] = {}
    unknown: list[str] = []
    for token in enabled:
        hit: Path | None = None
        for fname, path in available.items():
            if token in (fname, fname[:-3], _logical_name(fname)):
                hit = path
                break
        if hit is None:
            unknown.append(token)
        else:
            matched[hit.name] = hit
    return list(matched.values()), unknown


def resolve_snippets(
    *,
    bundled_dir: Path,
    user_dir: Path,
    repo_dir: Path,
    disabled: list[str],
    available_dir: Path | None = None,
    enabled: list[str] | None = None,
) -> list[Path]:
    """Return the ordered list of snippet files to execute at golden build.

    Precedence per filename: repo > user > (available, if enabled) > bundled.
    Names listed in ``disabled`` are dropped from the final set — matched by
    full filename, by filename without the ``.sh`` suffix, or by logical name
    (e.g. ``registry-mirror-ca`` drops ``90-registry-mirror-ca.sh``). Output is
    sorted lexically by filename.

    ``available_dir``/``enabled`` stage opt-in snippets from a bundled
    "available" library (see ``resolve_available``) between bundled and
    user precedence — only snippets named in ``enabled`` are staged.

    Missing dirs are treated as empty (no error). Empty files ARE kept —
    the runtime ``[ -s "$f" ]`` check inside install.sh skips them.
    """
    effective: dict[str, Path] = {}
    if bundled_dir.is_dir():
        for path in sorted(bundled_dir.glob("*.sh")):
            effective[path.name] = path
    if available_dir is not None and enabled:
        matched, _unknown = resolve_available(available_dir, enabled)
        for path in matched:
            effective[path.name] = path
    for d in (user_dir, repo_dir):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.sh")):
            effective[path.name] = path

    # Match disable entries by full filename, by filename without the ``.sh``
    # suffix, or by logical name (``registry-mirror-ca`` for
    # ``90-registry-mirror-ca.sh``) — symmetric with how ``enable_snippets``
    # resolves names via ``resolve_available``.
    disabled_norm = {d[:-3] if d.endswith(".sh") else d for d in disabled}
    for name in list(effective):
        stem = name[:-3] if name.endswith(".sh") else name
        if disabled_norm & {name, stem, _logical_name(name)}:
            del effective[name]

    return [effective[k] for k in sorted(effective)]


@dataclass(frozen=True)
class ArchivedImage:
    """A dated archive of the golden image: alias `<golden.alias>-YYYY-MM-DD`."""

    alias: str
    date: date
    size_bytes: int


def _classify_golden_images(
    incus: Incus, base_aliases: list[str]
) -> tuple[dict[str, int | None], dict[str, list[ArchivedImage]]]:
    """Classify golden images in one `list_images()` pass.

    Returns ``(live_size_by_base, archives_by_base)`` keyed by the
    de-duplicated ``base_aliases``. ``live_size_by_base[b]`` is the size of the
    exact-match live image ``b`` (None if absent); ``archives_by_base[b]`` holds
    the dated ``b-YYYY-MM-DD`` archives, newest-first.
    """
    bases = list(dict.fromkeys(base_aliases))  # dedup, preserve order
    live: dict[str, int | None] = {b: None for b in bases}
    archives: dict[str, list[ArchivedImage]] = {b: [] for b in bases}
    patterns = {b: re.compile(rf"^{re.escape(b)}-(\d{{4}}-\d{{2}}-\d{{2}})$") for b in bases}
    for img in incus.list_images():
        size = int(img.get("size", 0) or 0)
        for a in img.get("aliases", []):
            name = a.get("name", "")
            if name in live:  # exact match == the live base image
                live[name] = size
                continue
            for b, pattern in patterns.items():
                m = pattern.match(name)
                if m is not None:
                    archives[b].append(
                        ArchivedImage(
                            alias=name,
                            date=datetime.strptime(m.group(1), "%Y-%m-%d").date(),
                            size_bytes=size,
                        )
                    )
                    break
    for b in bases:
        archives[b].sort(key=lambda ai: ai.date, reverse=True)
    return live, archives


def find_all_archived_images(incus: Incus, base_aliases: list[str]) -> list[ArchivedImage]:
    """Return dated archives for every base alias, newest-first (one pass).

    Each archive is provably superseded by its live base alias, so all are safe
    prune candidates. The live aliases and unrelated images are ignored.
    """
    _live, archives = _classify_golden_images(incus, base_aliases)
    found = [ai for lst in archives.values() for ai in lst]
    found.sort(key=lambda ai: ai.date, reverse=True)
    return found


def find_archived_images(cfg: Config, incus: Incus) -> list[ArchivedImage]:
    """Return dated archive images (`<alias>-YYYY-MM-DD`), newest first.

    The live undated alias and unrelated images are ignored. Each archive is
    provably superseded by the live `cfg.golden.alias`, so all are safe prune
    candidates.
    """
    return find_all_archived_images(incus, [cfg.golden.alias])


@dataclass(frozen=True)
class GoldenImageUsage:
    """Disk usage of one golden base: the live image (if any) + its archives."""

    base_alias: str
    live_size_bytes: int | None
    archives: list[ArchivedImage]


def gather_golden_usage(incus: Incus, base_aliases: list[str]) -> list[GoldenImageUsage]:
    """Return per-base-alias usage (live image + dated archives), one per base.

    Bases are de-duplicated with input order preserved. A base that exists only
    as archives has ``live_size_bytes=None``.
    """
    live, archives = _classify_golden_images(incus, base_aliases)
    return [
        GoldenImageUsage(base_alias=b, live_size_bytes=live[b], archives=archives[b])
        for b in dict.fromkeys(base_aliases)
    ]


def build_golden_image(cfg: Config, incus: Incus) -> None:
    """Build the golden image from a fresh Ubuntu container.

    Steps:
      1. Launch a fresh Ubuntu container of the configured version
      2. Copy provision/install.sh into it
      3. Run it with config-driven env vars
      4. Stop, publish as the configured alias, delete the build container
    """
    image = f"images:ubuntu/{cfg.golden.ubuntu_version}"
    build_container = f"{cfg.container_prefix}-base-build"
    stacks = cfg.golden.stacks

    # Recover from a previous failed build that leaked the build container.
    # Without this, `incus launch` below trips a UNIQUE constraint failure
    # and the user is stuck until they clean up by hand.
    if incus.exists(build_container):
        warn(f"Removing leftover {build_container} from a previous failed build")
        incus.delete(build_container, force=True)

    info(f"Launching {image} as {build_container} on {LOOSE_BRIDGE}")
    # security.nesting=true is required for systemd-networkd to start in
    # the container: on Ubuntu 24.04+ hosts that set
    # kernel.apparmor_restrict_unprivileged_userns=1, the unprivileged user
    # namespace systemd (>=256) needs for DynamicUser=/PrivateUsers=
    # services is otherwise blocked. Without it the build container has no
    # IPv4 / DNS, and apt-get fails immediately. The runtime <prefix>-base
    # profile already has this flag, but `incus launch` here uses only the
    # default profile.
    #
    # network=jailbee-loose bypasses incusbr0's strict ACL — the build needs
    # unrestricted egress to fetch packages from archive.ubuntu.com,
    # NodeSource, Amazon Corretto, etc. The runtime ACL applies only to
    # working containers, not the one-time image build.
    incus.launch(
        image,
        build_container,
        config={"security.nesting": "true"},
        network=LOOSE_BRIDGE,
    )

    # Everything past launch must be wrapped — any failure (provisioning,
    # stop, publish) used to leak the container.
    try:
        if cfg.golden.provision_script is not None:
            script_path = cfg.golden.provision_script
            if not script_path.is_absolute():
                script_path = cfg.repo_root / script_path
            install_sh = script_path.read_text()
        else:
            install_sh = (
                importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
            )

        info("Copying provisioning script into container")
        incus.exec(build_container, ["mkdir", "-p", "/provision"])
        encoded = base64.b64encode(install_sh.encode()).decode()
        incus.exec(
            build_container,
            [
                "bash",
                "-c",
                f"echo '{encoded}' | base64 -d > /provision/install.sh && "
                "chmod +x /provision/install.sh",
            ],
        )

        # Stage user/repo install.d/ snippets when no provision_script override.
        if cfg.golden.provision_script is None:
            from jailbee.global_config import default_global_config_path
            from jailbee.paths import repo_config_dir_name

            # Derived from the global config path so `XDG_CONFIG_HOME` is
            # honoured exactly as `global_config` does — hardcoding ~/.config
            # would look in a directory that variable's user never writes.
            user_install_d = default_global_config_path().parent / "install.d"
            repo_install_d = cfg.repo_root / repo_config_dir_name(cfg.repo_root) / "install.d"
            bundled_install_d = importlib.resources.files("jailbee.provision").joinpath("install.d")
            available_install_d = importlib.resources.files("jailbee.provision").joinpath(
                "install.d.available"
            )
            # importlib.resources returns a Traversable; coerce to a Path
            # for resolve_snippets. May not exist on disk (zipimport edge);
            # resolve_snippets tolerates missing dirs.
            try:
                bundled_path = Path(str(bundled_install_d))
            except TypeError:
                bundled_path = Path("/nonexistent")
            try:
                available_path = Path(str(available_install_d))
            except TypeError:
                available_path = Path("/nonexistent")
            enabled = list(dict.fromkeys([*cfg.golden.enable_snippets, *stacks.snippet_names()]))
            # Warn only on user-supplied enable_snippets names; stack-derived
            # names are always valid library entries and must not trigger
            # spurious warnings.
            unknown_enabled = resolve_available(available_path, cfg.golden.enable_snippets)[1]
            for name in unknown_enabled:
                warn(f"golden.enable_snippets: no such snippet {name!r} — ignored")
            snippets = resolve_snippets(
                bundled_dir=bundled_path,
                user_dir=user_install_d,
                repo_dir=repo_install_d,
                disabled=cfg.golden.disable_snippets,
                available_dir=available_path,
                enabled=enabled,
            )
            if snippets:
                info(f"Staging {len(snippets)} snippet(s) into /provision/install.d/")
                incus.exec(build_container, ["mkdir", "-p", "/provision/install.d"])
                for snippet in snippets:
                    content = snippet.read_text()
                    snippet_encoded = base64.b64encode(content.encode()).decode()
                    target = f"/provision/install.d/{snippet.name}"
                    incus.exec(
                        build_container,
                        [
                            "bash",
                            "-c",
                            f"echo '{snippet_encoded}' | base64 -d > {target} && chmod +x {target}",
                        ],
                    )

        if cfg.golden.python:
            warn(
                "golden.python is deprecated and ignored; the container "
                "Python comes from the base image (golden.ubuntu_version). "
                "Remove the key from the golden: block."
            )
        stack_node_major = stacks.node_major()
        exec_env = {
            "CONTAINER_UID": str(cfg.container_user.uid),
            "CONTAINER_GID": str(cfg.container_user.gid),
            "JAVA_PACKAGE": stacks.java_package() or _java_apt_package(cfg.golden.java),
            "NODE_MAJOR": str(
                stack_node_major if stack_node_major is not None else cfg.golden.node
            ),
            # Space-joined for install.sh to apt-get install. Package names
            # are validated in config.py so the unquoted shell expansion is
            # safe from injection.
            "EXTRA_APT_PACKAGES": " ".join(cfg.golden.extra_apt_packages),
            "JAILBEE_USER_HOME": "/home/dev",
            "JAILBEE_PROVISION_DIR": "/provision",
            **cfg.golden.provision_env,
        }
        # The single longest step of the build, and the one with nothing to
        # print: install.sh's own apt output is captured, so without a live
        # line this is many minutes of a still terminal.
        with status_with_elapsed("running the provisioning script (several minutes)"):
            incus.exec(
                build_container,
                ["bash", "/provision/install.sh"],
                env=exec_env,
            )

        info("Stopping build container")
        incus.stop(build_container)

        today = datetime.now(UTC).strftime("%Y-%m-%d")

        # Archive the existing alias: without this, `incus publish
        # ... --alias gisgro-base` fails with "Aliases already exists" on every
        # rebuild. Per spec §4, the previous version stays accessible under
        # `<alias>-<YYYY-MM-DD>` so existing CoW-instantiated containers retain
        # their reference and the user can inspect the old image if needed.
        if incus.image_exists(cfg.golden.alias):
            archived = f"{cfg.golden.alias}-{today}"
            # Same-day rebuild: an archive already exists from an earlier build
            # today. Delete the whole archived image (not just its alias) so the
            # rename below can reclaim the name AND no aliasless image is left
            # dangling. If the archive is still in use by a container, Incus
            # refuses the delete — fall back to dropping just the alias so the
            # rename can proceed and the build does not fail.
            if incus.image_exists(archived):
                info(f"Replacing same-day archive {archived}")
                try:
                    incus.image_delete(archived)
                except IncusError:
                    warn(f"{archived} is in use; dropping its alias only")
                    incus.image_alias_delete(archived)
            info(f"Archiving previous {cfg.golden.alias} as {archived}")
            incus.image_alias_rename(cfg.golden.alias, archived)

        info(f"Publishing as {cfg.golden.alias} (built {today})")
        incus.publish(build_container, cfg.golden.alias)
    finally:
        info("Deleting build container")
        incus.delete(build_container, force=True)

    success(f"Golden image '{cfg.golden.alias}' built successfully")
