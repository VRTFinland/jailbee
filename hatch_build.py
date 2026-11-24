"""Embed source provenance in wheels and sdists without dirtying the checkout."""

import json
import sys
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ProvenanceHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        sys.path.insert(0, str(Path(self.root) / "src"))
        try:
            from jailbee.version_info import identity

            info = identity(Path(self.root) / "src" / "jailbee")
        finally:
            sys.path.pop(0)
        if info is None:
            return
        directory = Path(self.directory)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=directory, delete=False
        ) as generated:
            json.dump(info, generated)
        destination = "jailbee/_build_info.json"
        if self.target_name == "sdist":
            destination = "src/" + destination
        build_data["force_include"][generated.name] = destination
        self.generated = Path(generated.name)

    def finalize(self, version, build_data, artifact_path):
        if hasattr(self, "generated"):
            self.generated.unlink(missing_ok=True)
