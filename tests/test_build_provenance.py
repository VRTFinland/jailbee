"""Build-hook contract: generated artifacts, not edits to the checkout."""

import json
import runpy
import sys
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "target,destination",
    [("wheel", "jailbee/_build_info.json"), ("sdist", "src/jailbee/_build_info.json")],
)
def test_build_hook_stamps_distribution_only(tmp_path, monkeypatch, target, destination):
    interface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = object
    monkeypatch.setitem(sys.modules, interface.__name__, interface)
    hook_type = runpy.run_path(str(Path(__file__).parents[1] / "hatch_build.py"))["ProvenanceHook"]
    monkeypatch.setattr(
        "jailbee.version_info.identity", lambda package: {"commit": "d" * 40, "dirty": False}
    )
    hook = hook_type()
    hook.root = str(tmp_path)
    hook.directory = str(tmp_path / "dist")
    hook.target_name = target
    data = {"force_include": {}}
    hook.initialize("standard", data)
    (generated,) = data["force_include"]
    assert data["force_include"][generated] == destination
    assert json.loads(Path(generated).read_text()) == {"commit": "d" * 40, "dirty": False}
    assert not (tmp_path / "src/jailbee/_build_info.json").exists()
    hook.finalize("standard", data, "unused")
    assert not Path(generated).exists()


@pytest.mark.parametrize("version", ["editable", "standard"])
def test_build_without_identity_adds_no_metadata(tmp_path, monkeypatch, version):
    interface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = object
    monkeypatch.setitem(sys.modules, interface.__name__, interface)
    hook_type = runpy.run_path(str(Path(__file__).parents[1] / "hatch_build.py"))["ProvenanceHook"]
    monkeypatch.setattr("jailbee.version_info.identity", lambda package: None)
    hook = hook_type()
    hook.root = str(tmp_path)
    hook.directory = str(tmp_path / "dist")
    hook.target_name = "wheel"
    data = {"force_include": {}}
    hook.initialize(version, data)
    assert data == {"force_include": {}}
    hook.finalize(version, data, "unused")
