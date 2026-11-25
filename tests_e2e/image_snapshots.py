"""Pillow screenshot assertions; references are updated only by an explicit opt-in."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

# Chrome/FreeType versions can shade text edges differently. Ignore small
# per-channel differences, but never allow a layout shift or a changed swatch.
_CHANNEL_TOLERANCE = 24
_MAX_CHANGED_FRACTION = 0.002
_MAX_MEAN_DIFFERENCE = 0.25


def assert_image_matches(
    actual: bytes, *, reference: Path, output: Path, update: bool = False
) -> None:
    """Compare a PNG, retaining actual and amplified diff PNGs on failure."""
    if update:
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(actual)
        return

    if not reference.is_file():
        output.mkdir(parents=True, exist_ok=True)
        (output / "actual.png").write_bytes(actual)
        raise AssertionError(
            f"Missing gallery reference {reference}; review `make web-e2e-snapshots` output. "
            f"Actual: {output / 'actual.png'}"
        )

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
    changed = sum(histogram[_CHANNEL_TOLERANCE + 1 :]) / (canvas[0] * canvas[1])
    mean = sum(ImageStat.Stat(diff).mean) / 3
    if (
        image.size == baseline.size
        and changed <= _MAX_CHANGED_FRACTION
        and mean <= _MAX_MEAN_DIFFERENCE
    ):
        return

    output.mkdir(parents=True, exist_ok=True)
    actual_path, diff_path = output / "actual.png", output / "diff.png"
    actual_path.write_bytes(actual)
    diff.point(lambda value: min(255, value * 4)).save(diff_path)
    raise AssertionError(
        f"Gallery differs from {reference.name}: size {image.size} vs {baseline.size}, "
        f"{changed:.3%} pixels above {_CHANNEL_TOLERANCE}/255 "
        f"(limit {_MAX_CHANGED_FRACTION:.1%}), mean {mean:.3f} "
        f"(limit {_MAX_MEAN_DIFFERENCE}). Actual: {actual_path}; diff: {diff_path}. "
        "For an intended change, review `make web-e2e-snapshots` output."
    )
