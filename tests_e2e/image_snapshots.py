"""Pillow screenshot assertions; references are updated only by an explicit opt-in."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

# Chrome/FreeType versions can shade text edges differently. Ignore small
# per-channel differences, but never allow a layout shift or a changed swatch.
# The changed-pixel budget is a count, not a fraction: one recoloured chip or
# icon (a few hundred pixels) fails in a large capture as in a small one.
_CHANNEL_TOLERANCE = 24
_MAX_CHANGED_PIXELS = 200
_MAX_MEAN_DIFFERENCE = 0.25


def _write_reference(actual: bytes, reference: Path) -> None:
    """Store `actual` losslessly at Pillow's maximum deterministic compression.

    Lossy quantisation (pngquant) would add a mean error above the cap, so the
    references stay exact; the bloat is kept down by rewriting only references
    that no longer match (see `assert_image_matches`).
    """
    with Image.open(BytesIO(actual)) as captured:
        image = captured.convert("RGB")
    reference.parent.mkdir(parents=True, exist_ok=True)
    image.save(reference, format="PNG", optimize=True)


def assert_image_matches(
    actual: bytes, *, reference: Path, output: Path, update: bool = False
) -> None:
    """Compare a PNG, retaining actual and amplified diff PNGs on failure.

    With `update`, a reference that is missing or no longer matches is
    rewritten; one that still matches is left alone, so regeneration churns
    only what a design change touched.
    """
    if update:
        if not reference.is_file() or _difference(actual, reference) is not None:
            _write_reference(actual, reference)
        return

    if not reference.is_file():
        output.mkdir(parents=True, exist_ok=True)
        (output / "actual.png").write_bytes(actual)
        raise AssertionError(
            f"Missing gallery reference {reference}; review `make web-e2e-snapshots` output. "
            f"Actual: {output / 'actual.png'}"
        )

    failure = _difference(actual, reference)
    if failure is None:
        return
    message, diff = failure
    output.mkdir(parents=True, exist_ok=True)
    actual_path, diff_path = output / "actual.png", output / "diff.png"
    actual_path.write_bytes(actual)
    diff.point(lambda value: min(255, value * 4)).save(diff_path)
    raise AssertionError(
        f"Gallery differs from {reference.name}: {message}. "
        f"Actual: {actual_path}; diff: {diff_path}. "
        "For an intended change, review `make web-e2e-snapshots` output."
    )


def _difference(actual: bytes, reference: Path) -> tuple[str, Image.Image] | None:
    """None when `actual` matches `reference` within budget, else why and the diff."""
    with Image.open(BytesIO(actual)) as captured, Image.open(reference) as expected:
        image = captured.convert("RGB")
        baseline = expected.convert("RGB")
    # Padding a size mismatch makes the diagnostic diff useful even after a
    # gallery addition. Dimensions must still match exactly to pass.
    canvas = (max(image.width, baseline.width), max(image.height, baseline.height))
    actual_canvas = Image.new("RGB", canvas)
    actual_canvas.paste(image)
    expected_canvas = Image.new("RGB", canvas)
    expected_canvas.paste(baseline)
    diff = ImageChops.difference(actual_canvas, expected_canvas)
    red, green, blue = diff.split()
    largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    histogram = largest.histogram()
    changed = sum(histogram[_CHANNEL_TOLERANCE + 1 :])
    mean = sum(ImageStat.Stat(diff).mean) / 3
    if (
        image.size == baseline.size
        and changed <= _MAX_CHANGED_PIXELS
        and mean <= _MAX_MEAN_DIFFERENCE
    ):
        return None
    return (
        f"size {image.size} vs {baseline.size}, {changed} pixels above "
        f"{_CHANNEL_TOLERANCE}/255 (limit {_MAX_CHANGED_PIXELS}), mean {mean:.3f} "
        f"(limit {_MAX_MEAN_DIFFERENCE})",
        diff,
    )
