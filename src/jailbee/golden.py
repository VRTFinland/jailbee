"""Build the base golden Incus image (alias derived from container_prefix)."""

from __future__ import annotations

import base64
import importlib.resources
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path

import yaml

from jailbee.config import Config
from jailbee.incus import Incus, IncusError
from jailbee.init_command import LOOSE_BRIDGE, ensure_loose_bridge
from jailbee.stopping import stop_container
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
    repo_dir: Path | None,
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

    Missing dirs are treated as empty (no error), and so is a ``repo_dir`` of
    ``None`` — which is how a synthesized config says "this directory
    contributes nothing", the image it builds being shared host-wide. Empty
    files ARE kept — the runtime ``[ -s "$f" ]`` check inside install.sh skips
    them.
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
        if d is None or not d.is_dir():
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


def _resource_dir(name: str) -> Path:
    """Coerce a bundled `jailbee.provision` subdirectory to a Path.

    `importlib.resources` hands back a Traversable, which is not a Path under
    zipimport. `resolve_snippets` tolerates a missing directory, so an
    unusable resource degrades to "no snippets here" rather than an error.
    """
    trav = importlib.resources.files("jailbee.provision").joinpath(name)
    try:
        return Path(str(trav))
    except TypeError:
        return Path("/nonexistent")


def browser_snippet_names(cfg: Config) -> list[str]:
    """Provisioning snippets for browsers configured to come from the image.

    Derived from `browsers.<name>.source` rather than declared separately:
    a `golden.stacks.browsers` toggle would be a second way to say the same
    thing, and the two could disagree.
    """
    names: list[str] = []
    for name in cfg.browsers.enabled_names():
        if getattr(cfg.browsers, name).source == "image":
            names.append(f"70-{name}.sh")
    return names


def resolved_snippet_paths(cfg: Config) -> list[Path]:
    """The install.d snippets a golden build for `cfg` would run, in order.

    Empty when `golden.provision_script` is set: that path replaces
    install.sh wholesale and stages no snippets.

    Shared by `build_golden_image` (which needs the files) and
    `repo_uses_docker` (which needs their names), so "what would this repo's
    image contain" has exactly one answer.
    """
    if cfg.golden.provision_script is not None:
        return []

    from jailbee.global_config import default_global_config_path
    from jailbee.paths import repo_config_dir_name

    # Derived from the global config path so `XDG_CONFIG_HOME` is honoured
    # exactly as `global_config` does — hardcoding ~/.config would look in a
    # directory that variable's user never writes.
    user_install_d = default_global_config_path().parent / "install.d"
    # A synthesized config builds the *host-shared* `jailbee-scratch-base`
    # image (see `config.SCRATCH_BASE_ALIAS`). Reading this directory's
    # `install.d/` would put one directory's snippets into an image every
    # scratch container on the host runs — spec §6 says no per-directory input
    # reaches it. Contrived (a `.jailbee/install.d` with no `config.yaml`
    # beside it) but host-wide when it happens, so the guard is here rather
    # than in a comment.
    repo_install_d = (
        None
        if cfg.is_synthetic()
        else cfg.repo_root / repo_config_dir_name(cfg.repo_root) / "install.d"
    )
    enabled = list(
        dict.fromkeys(
            [
                *cfg.golden.enable_snippets,
                *cfg.golden.stacks.snippet_names(),
                *browser_snippet_names(cfg),
            ]
        )
    )
    return resolve_snippets(
        bundled_dir=_resource_dir("install.d"),
        user_dir=user_install_d,
        repo_dir=repo_install_d,
        disabled=cfg.golden.disable_snippets,
        available_dir=_resource_dir("install.d.available"),
        enabled=enabled,
    )


def resolved_snippet_names(cfg: Config) -> set[str]:
    """Logical names ('docker', 'nodejs') of `resolved_snippet_paths(cfg)`."""
    return {_logical_name(p.name) for p in resolved_snippet_paths(cfg)}


def repo_uses_docker(cfg: Config) -> bool:
    """Whether a golden image built for `cfg` would contain Docker.

    Answers a question about *image content* only — whether the repo wants the
    registry mirror is `docker_daemon.mirror_wanted`, which adds signals that
    say nothing about what is installed.

    Covers `golden.extra_apt_packages: [docker...]` (staged by the bundled
    `05-extra-apt.sh`, so it is independent of install.d resolution and applies
    with a `provision_script` too), `golden.stacks.docker`, the
    `golden.enable_snippets` escape hatch, a user's or repo's own
    `install.d/50-docker.sh`, and `disable_snippets` cancelling any of the
    snippet-borne ones. A custom snippet under a different name
    (`55-docker-ce.sh`) is invisible here — that is what
    `docker_registry_mirror.enabled: true` is for.
    """
    # apt package names are validated lowercase (config.py `_APT_PACKAGE_NAME_RE`),
    # so a plain prefix test covers docker.io / docker-ce / docker-compose-plugin.
    if any(pkg.startswith("docker") for pkg in cfg.golden.extra_apt_packages):
        return True
    if cfg.golden.provision_script is not None:
        return cfg.golden.stacks.docker
    return "docker" in resolved_snippet_names(cfg)


def golden_instance_name(alias: str) -> str:
    """Name of the stopped golden instance (instance mode)."""
    return f"{alias}--golden"


def golden_build_name(alias: str) -> str:
    """Name of the transient build container."""
    return f"{alias}--build"


@dataclass(frozen=True)
class GoldenPlacement:
    """Where the golden lives and which form it takes.

    ``pool``/``driver`` are None when no pool could be resolved (image mode,
    pool left to the profile).
    """

    pool: str | None
    driver: str | None

    @property
    def instance_mode(self) -> bool:
        return self.driver == "btrfs"


def _root_pool(yaml_text: str) -> str | None:
    """The `pool` of the root disk device (`path: /`) in an instance/profile YAML."""
    data = yaml.safe_load(yaml_text) if yaml_text else None
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict):
        return None
    for dev in devices.values():
        if isinstance(dev, dict) and dev.get("type") == "disk" and dev.get("path") == "/":
            pool = dev.get("pool")
            return str(pool) if pool else None
    return None


def resolve_golden_placement(cfg: Config, incus: Incus) -> GoldenPlacement:
    """Resolve the golden's pool and mode.

    The pool is `defaults.storage_pool` (validated; its `ValueError` propagates)
    else the root disk pool of the `default` profile.
    """
    from jailbee.lifecycle import check_storage_pool

    pool = cfg.defaults.storage_pool
    if pool:
        check_storage_pool(incus, pool)
    else:
        pool = _root_pool(incus.profile_show("default"))
    if not pool:
        return GoldenPlacement(None, None)
    driver = next(
        (str(p.get("driver")) for p in incus.list_storage_pools() if p.get("name") == pool),
        None,
    )
    return GoldenPlacement(pool, driver)


def golden_instance_pool(incus: Incus, name: str) -> str | None:
    """The pool of an instance's root disk, or None if it has none."""
    return _root_pool(incus.config_show(name, expanded=True))


def _best_effort(what: str, action: Callable[[], None]) -> bool:
    """Run a cleanup step; an `IncusError` is a warning, never a build failure."""
    try:
        action()
    except IncusError as exc:
        warn(f"Could not {what}: {exc}")
        return False
    return True


def _recover_leftovers(cfg: Config, incus: Incus) -> None:
    """Undo what an interrupted build left behind, before anything is created."""
    alias = cfg.golden.alias
    golden = golden_instance_name(alias)
    aside = f"{alias}--old"
    # The pre-1.x name of the build container, still recovered so an upgrade
    # does not inherit a leaked one under a name nothing else looks at.
    for build in (golden_build_name(alias), f"{cfg.container_prefix}-base-build"):
        if incus.exists(build):
            warn(f"Removing leftover {build} from a previous failed build")
            incus.delete(build, force=True)
    # A crash between "golden set aside" and "build promoted" leaves only the
    # aside copy: it is still the newest complete golden, so put it back.
    if incus.exists(aside):
        if incus.exists(golden):
            warn(f"Removing leftover {aside} from a previous build")
            incus.delete(aside, force=True)
        else:
            warn(f"Restoring {golden} from {aside} left by an interrupted build")
            incus.rename(aside, golden)
    temps = (f"{alias}--new", f"{alias}--old")
    # A crash between "alias set aside" and "new alias promoted" leaves no
    # image under the alias: promote the newest complete one (`--new` is
    # published whole before any alias moves) instead of deleting both.
    if not incus.image_exists(alias):
        for temp in temps:
            if incus.image_exists(temp):
                warn(f"Restoring image {alias} from {temp} left by an interrupted build")
                incus.image_alias_rename(temp, alias)
                break
    for temp in temps:
        if incus.image_exists(temp):
            warn(f"Removing leftover image {temp} from a previous build")
            incus.image_delete(temp)


def _remove_other_forms(cfg: Config, incus: Incus, *, instance_mode: bool) -> None:
    """Drop the golden's other form and every dated archive (best-effort)."""
    from jailbee.maintenance import humanize

    alias = cfg.golden.alias
    if instance_mode:
        if incus.image_exists(alias):
            _best_effort(f"delete the golden image {alias}", lambda: incus.image_delete(alias))
    else:
        golden = golden_instance_name(alias)
        if incus.exists(golden):
            _best_effort(
                f"delete the golden instance {golden}",
                lambda: incus.delete(golden, force=True),
            )
    removed = 0
    total = 0
    for archive in find_archived_images(cfg, incus):
        if _best_effort(
            f"delete the old golden archive {archive.alias}",
            partial(incus.image_delete, archive.alias),
        ):
            removed += 1
            total += archive.size_bytes
    if removed:
        info(f"Removed {removed} old golden archive(s), {humanize(total)}")


def build_golden_image(cfg: Config, incus: Incus) -> None:
    """Build the golden from a fresh Ubuntu container.

    Steps:
      1. Resolve the pool and mode (`resolve_golden_placement`) — an unknown
         pool fails here, before anything is created
      2. Recover leftovers of an interrupted build (build container, the
         golden instance set aside, temporary image aliases)
      3. Launch a fresh Ubuntu container of the configured version on that pool
      4. Copy provision/install.sh into it, run it with config-driven env vars,
         stop it
      5. Instance mode (btrfs): strip the build NIC and nesting, rename it to
         `<alias>--golden`, replacing the previous one. Image mode: publish
         as `<alias>--new` and swap the alias over, deleting the previous image
      6. Remove the golden's other form and any dated archives, best-effort
    """
    image = f"images:ubuntu/{cfg.golden.ubuntu_version}"
    alias = cfg.golden.alias
    build_container = golden_build_name(alias)
    golden = golden_instance_name(alias)
    aside = f"{alias}--old"
    stacks = cfg.golden.stacks

    placement = resolve_golden_placement(cfg, incus)

    # Recover from a previous failed build that leaked the build container.
    # Without this, `incus launch` below trips a UNIQUE constraint failure
    # and the user is stuck until they clean up by hand.
    _recover_leftovers(cfg, incus)

    info(
        f"Building {cfg.golden.alias} from {image} "
        f"(throwaway build container {build_container} on {LOOSE_BRIDGE})"
    )
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
    # working containers, not the one-time image build. A host that never
    # ran `jailbee init` (a scratch directory's first build) has no such
    # bridge yet, so create it here rather than fail on "Network not found".
    ensure_loose_bridge(incus, quiet=True)
    incus.launch(
        image,
        build_container,
        config={"security.nesting": "true"},
        network=LOOSE_BRIDGE,
        storage_pool=placement.pool,
    )

    # Everything past launch must be wrapped — any failure (provisioning,
    # stop, publish) used to leak the container. Once the build container has
    # become the golden instance it must survive the `finally`.
    promoted = False
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
            # Warn only on user-supplied enable_snippets names; stack-derived
            # names are always valid library entries and must not trigger
            # spurious warnings. This stays here rather than moving into
            # resolved_snippet_paths: it is a build-time diagnostic, and
            # `doctor` / `new` / `apply` must not print golden-build warnings.
            unknown_enabled = resolve_available(
                _resource_dir("install.d.available"), cfg.golden.enable_snippets
            )[1]
            for name in unknown_enabled:
                warn(f"golden.enable_snippets: no such snippet {name!r} — ignored")
            snippets = resolved_snippet_paths(cfg)
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

        # A clean shutdown is a nicety here, not a requirement: what gets
        # published is the filesystem, and the container is deleted moments
        # later either way. So when the build container's init will not go
        # down — a stuck apt-daily job, a process in D state — pull the plug
        # and publish anyway. Failing here instead used to throw away a
        # complete, successful provisioning run at the last step.
        stop_container(
            incus,
            build_container,
            force_fallback=True,
            label="the build container",
        )

        # One version only: deleting an image or golden instance never breaks
        # the containers made from it on btrfs, and ZFS defers the delete itself.
        if placement.instance_mode:
            # The build-time NIC (jailbee-loose) and nesting flag are build
            # needs; a copy of the golden takes its NIC and config from profiles.
            incus.config_device_remove(build_container, "eth0", missing_ok=True)
            incus.config_unset(build_container, "security.nesting")
            set_aside = False
            if incus.exists(golden):
                incus.rename(golden, aside)
                set_aside = True
            info(f"Promoting {build_container} to {golden}")
            try:
                incus.rename(build_container, golden)
            except BaseException:
                if set_aside:
                    _best_effort(
                        f"restore the previous golden {golden} from {aside}",
                        lambda: incus.rename(aside, golden),
                    )
                raise
            promoted = True
            if set_aside:
                _best_effort(
                    f"delete the previous golden {aside}",
                    lambda: incus.delete(aside, force=True),
                )
        else:
            new_alias = f"{alias}--new"
            old_alias = f"{alias}--old"
            info(f"Publishing as {alias}")
            incus.publish(build_container, new_alias)
            had_old = incus.image_exists(alias)
            if had_old:
                incus.image_alias_rename(alias, old_alias)
            try:
                incus.image_alias_rename(new_alias, alias)
            except BaseException:
                if had_old:
                    _best_effort(
                        f"restore the previous golden image {alias} from {old_alias}",
                        lambda: incus.image_alias_rename(old_alias, alias),
                    )
                raise
            if had_old:
                _best_effort(
                    f"delete the previous golden image {old_alias}",
                    lambda: incus.image_delete(old_alias),
                )
    finally:
        if not promoted:
            info("Deleting build container")
            incus.delete(build_container, force=True)

    _remove_other_forms(cfg, incus, instance_mode=placement.instance_mode)

    if placement.instance_mode:
        success(f"Golden instance '{golden}' built")
    else:
        success(f"Golden image '{alias}' built")
