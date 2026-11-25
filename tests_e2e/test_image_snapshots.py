"""The screenshot assertion's public contract and failure evidence."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageChops

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


def antialiased_ring(offset: float) -> bytes:
    """A dashed-edge-like antialiased ring, drawn at a subpixel offset."""
    from PIL import ImageDraw

    scale = 8
    big = Image.new("RGB", (60 * scale, 40 * scale), "white")
    shift = round(offset * scale)
    ImageDraw.Draw(big).ellipse(
        (10 * scale + shift, 6 * scale + shift, 50 * scale + shift, 34 * scale + shift),
        outline="black",
        width=2 * scale,
    )
    stream = BytesIO()
    big.resize((60, 40), Image.Resampling.BOX).save(stream, format="PNG")
    return stream.getvalue()


def test_a_subpixel_shift_of_an_edge_is_not_a_change(tmp_path: Path) -> None:
    """An unrelated change elsewhere on the page can move a group by a fraction
    of a pixel, re-shading every curved edge; a pixel whose value the reference
    has right beside it is not counted as changed."""
    reference, output = tmp_path / "ring.png", tmp_path / "evidence"
    reference.write_bytes(antialiased_ring(0))
    shifted = antialiased_ring(0.25)
    with Image.open(BytesIO(shifted)) as a, Image.open(reference) as b:
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
        assert sum(diff.histogram()[25:]) > 20  # the plain per-pixel budget would fail
    assert_image_matches(shifted, reference=reference, output=output)


def test_a_recoloured_stroke_is_a_change_even_beside_both_colours(tmp_path: Path) -> None:
    """Black ink on white turned blue: every neighbour is black or white, none
    blue, so the neighbourhood rule still counts each stroke pixel."""
    reference, output = tmp_path / "ring.png", tmp_path / "evidence"
    reference.write_bytes(antialiased_ring(0))
    with Image.open(BytesIO(antialiased_ring(0))) as image:
        recoloured = image.convert("RGB")
    pixels = recoloured.load()
    assert pixels is not None
    for x in range(recoloured.width):
        for y in range(recoloured.height):
            if pixels[x, y] == (0, 0, 0):
                pixels[x, y] = (0, 0, 255)
    stream = BytesIO()
    recoloured.save(stream, format="PNG")
    with pytest.raises(AssertionError, match="Gallery differs"):
        assert_image_matches(stream.getvalue(), reference=reference, output=output)


@pytest.mark.parametrize("vanishes", [True, False], ids=["vanishes", "appears"])
def test_a_thin_stroke_vanishing_or_appearing_is_a_change(tmp_path: Path, vanishes: bool) -> None:
    """A 2px icon stroke drawn in its background colour (black on black): each
    stroke pixel has background beside it, so only comparing both ways round
    sees it go, or come."""
    from PIL import ImageDraw

    with_stroke = Image.new("RGB", (40, 40), "black")
    ImageDraw.Draw(with_stroke).rectangle((10, 19, 29, 20), fill="white")
    ImageDraw.Draw(with_stroke).rectangle((19, 10, 20, 29), fill="white")
    images = []
    for image in (with_stroke, Image.new("RGB", (40, 40), "black")):
        stream = BytesIO()
        image.save(stream, format="PNG")
        images.append(stream.getvalue())
    reference_bytes, actual = images if vanishes else images[::-1]
    reference, output = tmp_path / "plus.png", tmp_path / "evidence"
    reference.write_bytes(reference_bytes)
    with pytest.raises(AssertionError, match="Gallery differs"):
        assert_image_matches(actual, reference=reference, output=output)


def test_missing_reference_fails_without_creating_it(tmp_path: Path) -> None:
    reference, output = tmp_path / "missing.png", tmp_path / "evidence"
    actual = png("white")
    with pytest.raises(AssertionError, match="Missing gallery reference"):
        assert_image_matches(actual, reference=reference, output=output)
    assert not reference.exists()
    assert (output / "actual.png").read_bytes() == actual
