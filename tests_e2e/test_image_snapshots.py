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


def pixels(data: bytes) -> tuple[tuple[int, int], bytes]:
    with Image.open(BytesIO(data)) as image:
        return image.size, image.convert("RGB").tobytes()


def with_changed_pixels(
    base: bytes, count: int, colour: tuple[int, int, int] = (200, 200, 200)
) -> bytes:
    with Image.open(BytesIO(base)) as image:
        changed = image.convert("RGB")
    for i in range(count):
        changed.putpixel((i % changed.width, i // changed.width), colour)
    stream = BytesIO()
    changed.save(stream, format="PNG")
    return stream.getvalue()


def test_reference_update_only_writes_the_reference(tmp_path: Path) -> None:
    reference, output = tmp_path / "references" / "sample.png", tmp_path / "evidence"
    original, changed = png("white"), png("black")
    assert_image_matches(original, reference=reference, output=output, update=True)
    assert_image_matches(changed, reference=reference, output=output, update=True)
    assert pixels(reference.read_bytes()) == pixels(changed)
    assert not output.exists()
    assert_image_matches(changed, reference=reference, output=output)
    assert not output.exists()


def test_update_writes_a_lossless_compressed_deterministic_reference(tmp_path: Path) -> None:
    stream = BytesIO()
    noisy = Image.effect_noise((120, 80), 40).convert("RGB")
    noisy.save(stream, format="PNG", compress_level=0)
    actual = stream.getvalue()
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    assert_image_matches(actual, reference=first, output=tmp_path / "out", update=True)
    assert_image_matches(actual, reference=second, output=tmp_path / "out", update=True)
    assert first.read_bytes() == second.read_bytes()
    assert pixels(first.read_bytes()) == pixels(actual)
    plain = BytesIO()
    noisy.save(plain, format="PNG")
    assert len(first.read_bytes()) <= len(plain.getvalue()) < len(actual)


def test_update_keeps_a_reference_that_still_matches(tmp_path: Path) -> None:
    """Regenerating rewrites only what changed, so a later design change does
    not churn every unchanged group's PNG through rasterisation noise."""
    reference, output = tmp_path / "sample.png", tmp_path / "evidence"
    reference.write_bytes(with_changed_pixels(png("white"), 3, (240, 240, 240)))
    kept = reference.read_bytes()
    assert_image_matches(png("white"), reference=reference, output=output, update=True)
    assert reference.read_bytes() == kept
    assert_image_matches(png("black"), reference=reference, output=output, update=True)
    assert pixels(reference.read_bytes()) == pixels(png("black"))


@pytest.mark.parametrize("actual", [png("black"), png("white", (101, 100))])
def test_mismatch_preserves_reference_and_writes_actual_and_diff(
    tmp_path: Path, actual: bytes
) -> None:
    reference, output = tmp_path / "sample.png", tmp_path / "evidence"
    original = png("white")
    reference.write_bytes(original)
    with pytest.raises(AssertionError, match=r"Gallery differs.*Actual:.*diff:"):
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


@pytest.mark.parametrize("size", [(100, 100), (1000, 400)])
@pytest.mark.parametrize("changed_pixels", [20, 21])
def test_changed_pixel_budget_is_absolute_whatever_the_image_size(
    tmp_path: Path, size: tuple[int, int], changed_pixels: int
) -> None:
    """A recoloured 16px icon is about 36 pixels: it must fail in a large
    capture as in a small one, so the budget is a pixel count, not a fraction.
    The mean error stays below its cap in both sizes."""
    reference, output = tmp_path / "sample.png", tmp_path / "evidence"
    reference.write_bytes(png("white", size))
    actual = with_changed_pixels(png("white", size), changed_pixels, (220, 220, 220))
    if changed_pixels <= 20:
        assert_image_matches(actual, reference=reference, output=output)
    else:
        with pytest.raises(AssertionError, match="Gallery differs"):
            assert_image_matches(actual, reference=reference, output=output)


def test_missing_reference_fails_without_creating_it(tmp_path: Path) -> None:
    reference, output = tmp_path / "missing.png", tmp_path / "evidence"
    actual = png("white")
    with pytest.raises(AssertionError, match="Missing gallery reference"):
        assert_image_matches(actual, reference=reference, output=output)
    assert not reference.exists()
    assert (output / "actual.png").read_bytes() == actual
