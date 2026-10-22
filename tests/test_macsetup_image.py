"""Tests for the machine-image step. Fully mocked."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from jailbee.macsetup.engine import MacPaths, SetupContext, SetupOptions, SetupState
from jailbee.macsetup.steps_image import IMAGE_TAG, ImageStep


class FakeAC:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.built: list[dict[str, object]] = []

    def image_exists(self, tag: str) -> bool:
        return self.exists

    def build_image(self, context: Path, tag: str, *, cpus: int, memory: str) -> None:
        self.built.append(
            {
                "tag": tag,
                "files": sorted(p.name for p in context.iterdir()),
                "cpus": cpus,
                "memory": memory,
            }
        )


def make_ctx(tmp_path: Path, ac: FakeAC) -> SetupContext:
    paths = MacPaths(tmp_path, tmp_path / "s.json", tmp_path / "k", tmp_path / "ssh")
    return SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(options=SetupOptions(), facts={"cpus": "6", "memory": "12G"}),
        paths=paths,
        interactive=True,
        confirm=lambda q: True,
        log=lambda line: None,
    )


def test_fingerprint_changes_with_packages(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    one = ImageStep(files={"Dockerfile": "FROM x", "packages.txt": "git\n"}).fingerprint(ctx)
    two = ImageStep(files={"Dockerfile": "FROM x", "packages.txt": "git\ncurl\n"}).fingerprint(ctx)
    assert one != two


def test_fingerprint_marks_a_removed_image_as_missing(tmp_path: Path) -> None:
    files = {"Dockerfile": "FROM x", "packages.txt": "git\n"}
    present = ImageStep(files=files).fingerprint(make_ctx(tmp_path, FakeAC(exists=True)))
    gone = ImageStep(files=files).fingerprint(make_ctx(tmp_path, FakeAC(exists=False)))
    assert present != gone


def test_build_uses_capped_builder_resources_and_records_tag(tmp_path: Path) -> None:
    ac = FakeAC()
    ctx = make_ctx(tmp_path, ac)
    step = ImageStep(files={"Dockerfile": "FROM x", "packages.txt": "git\n"})
    step.run(ctx)
    built = ac.built[0]
    assert built["files"] == ["Dockerfile", "packages.txt"]
    assert built["cpus"] == 4 and built["memory"] == "4G"
    assert str(built["tag"]).startswith(f"{IMAGE_TAG}:")
    assert ctx.state.facts["image"] == built["tag"]


def test_dockerfile_installs_packages_txt_and_netcat_is_listed() -> None:
    root = resources.files("jailbee.provision").joinpath("mac")
    assert "packages.txt" in root.joinpath("Dockerfile").read_text()
    packages = root.joinpath("packages.txt").read_text().split()
    assert {"netcat-openbsd", "incus", "btrfs-progs", "openssh-server"} <= set(packages)
