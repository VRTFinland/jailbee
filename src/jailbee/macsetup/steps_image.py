"""Build the Ubuntu image the machine boots from (packages and systemd defaults only)."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

from jailbee.macsetup.engine import SetupContext

IMAGE_TAG = "local/jailbee-machine"
_FILES = ("Dockerfile", "packages.txt")
BUILDER_CPUS = 4
BUILDER_MEMORY = "4G"


def _packaged_files() -> dict[str, str]:
    root = resources.files("jailbee.provision").joinpath("mac")
    return {name: root.joinpath(name).read_text() for name in _FILES}


class ImageStep:
    id = "image"
    title = "Building the machine image"

    def __init__(self, files: Mapping[str, str] | None = None) -> None:
        self._files = dict(files) if files is not None else _packaged_files()

    def _digest(self) -> str:
        data = "".join(f"{name}\0{self._files[name]}\0" for name in _FILES)
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    def _tag(self) -> str:
        return f"{IMAGE_TAG}:{self._digest()}"

    def fingerprint(self, ctx: SetupContext) -> str:
        # A removed image reads as "missing", so a re-run rebuilds it.
        return self._digest() if ctx.ac.image_exists(self._tag()) else f"{self._digest()}+missing"

    def run(self, ctx: SetupContext) -> None:
        tag = self._tag()
        cpus = min(int(ctx.state.facts["cpus"]), BUILDER_CPUS)
        with tempfile.TemporaryDirectory() as context:
            for name in _FILES:
                (Path(context) / name).write_text(self._files[name])
            ctx.ac.build_image(Path(context), tag, cpus=cpus, memory=BUILDER_MEMORY)
        ctx.state.facts["image"] = tag
