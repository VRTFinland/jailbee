"""The screenshot assertion's public contract and failure evidence."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from tests_e2e.image_snapshots import assert_image_matches


def png(colour: str, size: tuple[int, int] = (100, 100)) -> bytes:
    stream = BytesIO()
    Image.new("RGB", size, colour).save(stream, format="PNG")
    return stream.getvalue()


def test_reference_update_only_writes_the_reference(tmp_path: Path) -> None:
    reference, output = tmp_path / "references" / "sample.png", tmp_path / "evidence"
    original, changed = png("white"), png("black")
    assert_image_matches(original, reference=reference, output=output, update=True)
    assert_image_matches(changed, reference=reference, output=output, update=True)
    assert reference.read_bytes() == changed
    assert not output.exists()
    assert_image_matches(changed, reference=reference, output=output)
    assert not output.exists()


@pytest.mark.parametrize("actual", [png("black"), png("white", (101, 100))])
def test_mismatch_preserves_reference_and_writes_actual_and_diff(
    tmp_path: Path, actual: bytes
) -> None:
    reference, output = tmp_path / "sample.png", tmp_path / "evidence"
    original = png("white")
    reference.write_bytes(original)
    with pytest.raises(AssertionError, match="Gallery differs.*Actual:.*diff:"):
        assert_image_matches(actual, reference=reference, output=output)
    assert reference.read_bytes() == original
    assert (output / "actual.png").read_bytes() == actual
    with Image.open(output / "diff.png") as diff:
        assert diff.getbbox() is not None


def test_small_rasterisation_noise_is_allowed_but_broad_colour_drift_is_not(
    tmp_path: Path,
) -> None:
    reference, output = tmp_path / "sample.png", tmp_path / "evidence"
    reference.write_bytes(png("#ffffff"))
    with Image.open(BytesIO(png("white"))) as image:
        image.putpixel((0, 0), (232, 232, 232))
        stream = BytesIO()
        image.save(stream, format="PNG")
    assert_image_matches(stream.getvalue(), reference=reference, output=output)
    assert not output.exists()
    # A one-channel-level drift over the whole image is below the per-pixel
    # threshold, but must not disappear into it: the mean-error cap catches it.
    with pytest.raises(AssertionError, match="Gallery differs"):
        assert_image_matches(png("#fefefe"), reference=reference, output=output)


def test_missing_reference_fails_without_creating_it(tmp_path: Path) -> None:
    reference, output = tmp_path / "missing.png", tmp_path / "evidence"
    actual = png("white")
    with pytest.raises(AssertionError, match="Missing gallery reference"):
        assert_image_matches(actual, reference=reference, output=output)
    assert not reference.exists()
    assert (output / "actual.png").read_bytes() == actual
