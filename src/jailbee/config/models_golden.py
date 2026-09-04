"""Golden-image models: the language/tool stack toggles, the low-level
provisioning knobs, and the set of supported JetBrains IDE launchers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jailbee.config.models_host import SharedCache

# Provisioning env vars set automatically by `jailbee base build`. Users may not
# override these via `golden.provision_env` — built-in install.sh relies on
# them, and silently letting the user shadow them produces confusing failures.
_RESERVED_PROVISION_ENV_KEYS = frozenset(
    {
        "CONTAINER_UID",
        "CONTAINER_GID",
        "JAVA_PACKAGE",
        "NODE_MAJOR",
        "EXTRA_APT_PACKAGES",
        "JAILBEE_USER_HOME",
        "JAILBEE_PROVISION_DIR",
    }
)

# Debian package-name grammar (simplified): start with [a-z0-9], then
# [a-z0-9+\-.]. We enforce this on `golden.extra_apt_packages` because the
# values are passed unquoted to `apt-get install` inside install.sh — letting
# whitespace or shell metacharacters through would amount to shell injection.
_APT_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+\-.]*$")

# Java stack vendor and version format: openjdk-N or corretto-N (N is a major version).
_JAVA_STACK_RE = re.compile(r"^(openjdk|corretto)-\d+$")

# Default Node.js major version when node=True in stacks. Mirrors Golden.node default of 24.
_DEFAULT_NODE_MAJOR = 24


class Stacks(BaseModel):
    """High-level language/tool toggles for the golden image.

    Each enabled stack expands to its provisioning snippet, shared caches,
    and build-env values (see the derivation methods). This is sugar over
    ``golden.enable_snippets`` + ``shared_caches``; those remain the
    low-level escape hatch.
    """

    model_config = ConfigDict(extra="forbid")

    # bool comes first in each union so YAML `true`/`false` bind to bool,
    # not to a coerced int/str.
    java: bool | str = Field(
        default=False,
        description=(
            "Install a JDK in the golden image. `true` installs the default JDK "
            "(OpenJDK); a string such as `openjdk-21` or `corretto-17` pins the "
            "distribution and major version; `false` installs none."
        ),
    )
    node: bool | int = Field(
        default=False,
        description=(
            "Install Node.js in the golden image. `true` installs the default major "
            "version (24); an int such as `20` pins a specific major version; `false` "
            "installs none."
        ),
    )
    python: bool = Field(
        default=False,
        description="Install Python venv/pip tooling in the golden image.",
    )
    docker: bool = Field(
        default=False,
        description=(
            "Install Docker Engine in the golden image and add the container user to "
            "the `docker` group."
        ),
    )
    ecr: bool = Field(
        default=False,
        description=(
            "Install the Amazon ECR credential helper in the golden image, for "
            "authenticating `docker pull` against ECR."
        ),
    )

    @field_validator("java")
    @classmethod
    def _validate_java(cls, v: bool | str) -> bool | str:
        if isinstance(v, bool):
            return v
        if not _JAVA_STACK_RE.match(v):
            raise ValueError(
                f"invalid golden.stacks.java: {v!r}. Use 'openjdk-<N>', 'corretto-<N>', or true."
            )
        return v

    @field_validator("node")
    @classmethod
    def _validate_node(cls, v: bool | int) -> bool | int:
        if isinstance(v, bool):
            return v
        if v < 1:
            raise ValueError(
                f"invalid golden.stacks.node: {v!r}. Use a major version >= 1 or true."
            )
        return v

    def _java_vendor_version(self) -> tuple[str, str] | None:
        """(vendor, version) for a pinned ``java`` value, or None when java is
        off or ``True`` (no explicit vendor/version)."""
        if not isinstance(self.java, str):
            return None
        vendor, _, version = self.java.partition("-")
        return vendor, version

    def snippet_names(self) -> list[str]:
        """Bundled available-library base names implied by the enabled stacks."""
        names: list[str] = []
        if self.java:
            vv = self._java_vendor_version()
            names.append("20-corretto" if vv and vv[0] == "corretto" else "20-openjdk")
        if self.node:
            names.append("30-nodejs")
        if self.python:
            names.append("40-python")
        if self.docker:
            names.append("50-docker")
        if self.ecr:
            names.append("80-ecr-helper")
        if self.java and self.docker:
            names.append("90-registry-mirror-ca")
        return names

    def java_package(self) -> str | None:
        """apt package name for the java stack, or None when java is off."""
        if not self.java:
            return None
        vv = self._java_vendor_version()
        if vv is None:  # java is True → distro default JDK
            return "default-jdk"
        vendor, version = vv
        if vendor == "openjdk":
            return f"openjdk-{version}-jdk"
        # corretto — the only other vendor _JAVA_STACK_RE admits
        return f"java-{version}-amazon-corretto-jdk"

    def node_major(self) -> int | None:
        """node major version for the node stack, or None when node is off."""
        if self.node is True:
            return _DEFAULT_NODE_MAJOR
        if self.node is False:
            return None
        return self.node

    def shared_caches(self) -> list[SharedCache]:
        """Language caches implied by the enabled stacks."""
        caches: list[SharedCache] = []
        if self.java:
            caches.append(
                SharedCache(
                    name="gradle",
                    host_subpath="caches/gradle",
                    container_path="~/.gradle",
                )
            )
            caches.append(SharedCache(name="m2", host_subpath="caches/m2", container_path="~/.m2"))
        if self.node:
            caches.append(
                SharedCache(
                    name="npm",
                    host_subpath="caches/npm",
                    container_path="~/.npm",
                )
            )
            caches.append(
                SharedCache(
                    name="pnpm-store",
                    host_subpath="caches/pnpm-store",
                    container_path="~/.local/share/pnpm/store",
                )
            )
        return caches


class Golden(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(
        default="",
        description=(
            "Image alias `jailbee base build` publishes to and containers boot from. "
            "Empty (default) resolves to `<container_prefix>-base`."
        ),
    )
    ubuntu_version: str = Field(
        default="26.04",
        description="Ubuntu release tag for the base image, pulled from the `images:` remote.",
    )
    java: str = Field(
        default="amazon-corretto-17",
        description=(
            "Java package identifier for the `openjdk`/`corretto` snippet. "
            "`amazon-corretto-N` maps to apt package `java-N-amazon-corretto-jdk`; "
            "anything else is passed through as an apt package name. Only takes effect "
            "when that snippet is staged via `stacks.java` or `enable_snippets`."
        ),
    )
    node: int = Field(
        default=24,
        description=(
            "Node.js major version for the `nodejs` snippet (used by NodeSource). Only "
            "takes effect when that snippet is staged via `stacks.node` or "
            "`enable_snippets`."
        ),
    )
    # Kept in the model (rather than dropped as an extra-field error) so a
    # stale `python:` key is a soft, non-blocking deprecation warning (via
    # validate_runtime) instead of a hard config-load failure.
    python: str = Field(
        default="",
        description=(
            "Deprecated and ignored: the container's Python is always the base image's "
            "system `python3`, fixed by `ubuntu_version`. Setting this only raises a "
            "soft warning. Add a different Python via `extra_apt_packages` instead."
        ),
    )
    provision_script: Path | None = Field(
        default=None,
        description=(
            "Path to an alternative provisioning script, replacing the bundled "
            "`install.sh`. Relative paths resolve against the repo root."
        ),
    )
    provision_env: dict[str, str] = Field(
        default={},
        description=(
            "Extra environment variables passed to the provisioning script. Keys "
            "reserved for `install.sh`'s own use (e.g. `CONTAINER_UID`, `JAVA_PACKAGE`) "
            "are rejected at load time."
        ),
    )
    extra_apt_packages: list[str] = Field(
        default_factory=list,
        description=(
            "Extra apt package names installed by the bundled extra-apt snippet. Each "
            "must start with a lowercase letter or digit and contain only lowercase "
            "letters, digits, `+`, `-`, and `.`."
        ),
    )
    disable_snippets: list[str] = Field(
        default_factory=list,
        description=(
            "Snippet names dropped from the effective provisioning set, whether "
            "bundled by default or added via `stacks`/`enable_snippets`. Accepts the "
            "logical name, the numbered name, or the full filename."
        ),
    )
    enable_snippets: list[str] = Field(
        default_factory=list,
        description=(
            "Opt-in `install.d.available/` snippet names staged into the effective "
            "set, unioned with whatever `stacks` implies. Unknown names are ignored "
            "with a warning."
        ),
    )
    stacks: Stacks = Field(
        default_factory=Stacks,
        description=(
            "High-level `java`/`node`/`python`/`docker`/`ecr` toggles — the recommended "
            "way to enable a runtime, expanding to the matching snippet(s), shared "
            "caches, and build-env values."
        ),
    )

    @field_validator("extra_apt_packages")
    @classmethod
    def _validate_pkg_names(cls, v: list[str]) -> list[str]:
        for pkg in v:
            if not _APT_PACKAGE_NAME_RE.match(pkg):
                raise ValueError(
                    f"invalid apt package name: {pkg!r}. Must match "
                    r"[a-z0-9][a-z0-9+\-.]*"
                )
        return v


# Supported JetBrains Toolbox launcher names. The Toolbox lays each app out as
# /opt/jetbrains-toolbox/apps/<id>/bin/<launcher>, where the launcher binary
# uses the IDE's short name (e.g. `pycharm` for pycharm-professional, `idea`
# for intellij-idea-ultimate, `studio` for android-studio). gui.open_ide() uses
# this value directly as the `find -name` pattern.
IdeName = Literal[
    "idea",
    "webstorm",
    "pycharm",
    "goland",
    "clion",
    "phpstorm",
    "rider",
    "rubymine",
    "datagrip",
    "rustrover",
    "aqua",
    "dataspell",
    "studio",
]
