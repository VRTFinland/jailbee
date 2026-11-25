"""Pillow screenshot assertions; references are updated only by an explicit opt-in."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageMath, ImageStat

# Chrome/FreeType versions can shade text edges differently. Ignore small
# per-channel differences, but never allow a layout shift or a changed swatch.
# The changed-pixel budget is a count, not a fraction: one recoloured chip
# (about a thousand pixels) or small 16px icon (about 36) fails in a large
# capture as in a small one. A change elsewhere on the page can move a group
# by a fraction of a pixel and re-shade its curved edges, so both budgets
# measure how far a pixel lies outside the colour range of its reference
# neighbourhood (`_outside_neighbourhood`). A uniform drift is outside it on
# every flat area, which the mean cap catches.
_CHANNEL_TOLERANCE = 24
_MAX_CHANGED_PIXELS = 20
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


def _projections(image: Image.Image) -> list[Image.Image]:
    """R, G, B and the halved colour differences R-G, G-B, B-R as L images.

    Every one is linear in the colour, so a blend of two colours lies between
    them in each; the differences tell hues apart that R, G and B ranges do not
    (blue is within black-to-white in every channel, not in R-B).
    """
    red, green, blue = image.split()

    def half_difference(a: Image.Image, b: Image.Image) -> Image.Image:
        result = ImageMath.lambda_eval(
            lambda args: args["convert"]((args["a"] - args["b"] + 255) / 2, "L"), a=a, b=b
        )
        assert isinstance(result, Image.Image)
        return result

    return [
        red,
        green,
        blue,
        half_difference(red, green),
        half_difference(green, blue),
        half_difference(blue, red),
    ]


def _outside_neighbourhood(actual: Image.Image, reference: Image.Image) -> Image.Image:
    """Per pixel, how far its colour lies outside the range the reference's 3x3
    neighbourhood spans, in the worst projection (colour differences doubled
    back to channel units).

    An edge re-shaded by a subpixel shift is a blend of colours beside it, so
    within that range; a recoloured stroke, chip or icon is not.
    """
    worst = Image.new("L", actual.size, 0)
    pairs = zip(_projections(actual), _projections(reference), strict=True)
    for index, (value, around) in enumerate(pairs):
        low = around.filter(ImageFilter.MinFilter(3))
        high = around.filter(ImageFilter.MaxFilter(3))
        excess = ImageChops.lighter(
            ImageChops.subtract(low, value), ImageChops.subtract(value, high)
        )
        if index >= 3:
            excess = ImageChops.add(excess, excess)
        worst = ImageChops.lighter(worst, excess)
    return worst


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
    # Both ways round: a thin stroke that appears lies outside the reference's
    # range; one that vanishes leaves the reference outside the actual's.
    outside = ImageChops.lighter(
        _outside_neighbourhood(actual_canvas, expected_canvas),
        _outside_neighbourhood(expected_canvas, actual_canvas),
    )
    changed = sum(outside.histogram()[_CHANNEL_TOLERANCE + 1 :])
    mean = ImageStat.Stat(outside).mean[0]
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
