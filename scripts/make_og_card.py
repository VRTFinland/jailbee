#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11", "fonttools>=4.53", "brotli>=1.1"]
# ///
"""Render website/assets/img/jailbee-og.png, the site's link-preview card.

Run it after changing the tagline, the mark or the palette:

    uv run scripts/make_og_card.py

Dependencies are declared inline (PEP 723) rather than in the dev group,
because this runs about once a year and nobody should pay for Pillow on
every `uv sync`.

Why the card is generated rather than drawn by hand: it has to stay in step
with the site's palette, its typography and its lockup rule, and a hand-made
PNG drifts from all three silently. Everything below is read from what the
site already ships.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fontTools.ttLib.woff2 import decompress
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "website"
IMG = SITE / "assets" / "img"
FONTS = SITE / "assets" / "fonts"
OUT = IMG / "jailbee-og.png"

# The site's own tokens, copied from assets/style.css :root. Kept in sync by
# eye — there are five, and the card is regenerated rarely enough that
# parsing the stylesheet would be more machinery than the problem deserves.
GROUND = "#0b0c0e"
TEXT = "#e8e6e3"
AMBER = "#f0a92b"
MUTED = "#a2a7b0"
BORDER = "#262a32"

# Not a design choice: 1.91:1 is the ratio that makes Slack, Signal, LinkedIn
# and X render a large card. At any other ratio they fall back to a small
# square thumbnail, which is what this file exists to stop happening.
WIDTH, HEIGHT = 1200, 630

# Those platforms each crop the card differently, so nothing meaningful goes
# nearer the edge than this.
SAFE_MARGIN = 72

TAGLINE = "One container per branch."
URL = "jailbee.gisgro.io"

# The lockup rule from index.html: the "ai" of JailBee is set in the body
# colour against the amber of the rest — jAIlbee.
WORDMARK = (("J", AMBER), ("ai", TEXT), ("lBee", AMBER))


def _ttf(woff2_name: str, workdir: Path) -> str:
    """The site ships woff2; Pillow reads TrueType. Convert on the fly."""
    out = workdir / woff2_name.replace(".woff2", ".ttf")
    decompress(FONTS / woff2_name, out)
    return str(out)


def render(workdir: Path) -> None:
    card = Image.new("RGB", (WIDTH, HEIGHT), GROUND)
    draw = ImageDraw.Draw(card)

    f_word = ImageFont.truetype(_ttf("IBMPlexSans-SemiBold.woff2", workdir), 130)
    f_tag = ImageFont.truetype(_ttf("IBMPlexSans-Regular.woff2", workdir), 47)
    f_url = ImageFont.truetype(_ttf("IBMPlexMono-Regular.woff2", workdir), 27)

    mark = Image.open(IMG / "jailbee-mark.png").convert("RGBA")
    mark_h = 372
    mark_w = round(mark.width * mark_h / mark.height)
    mark = mark.resize((mark_w, mark_h), Image.LANCZOS)

    gap = 64
    word_w = sum(round(draw.textlength(t, font=f_word)) for t, _ in WORDMARK)
    text_w = max(word_w, round(draw.textlength(TAGLINE, font=f_tag)))

    # Centre the mark and the text block as one group. Anchoring it left
    # instead leaves the slack on the right, where it reads as a mistake
    # rather than as margin.
    group_w = mark_w + gap + text_w
    left = (WIDTH - group_w) // 2
    if left < SAFE_MARGIN:
        raise SystemExit(
            f"content is {group_w}px wide, leaving {left}px of margin — "
            f"under the {SAFE_MARGIN}px platforms may crop into. "
            "Shorten the tagline or shrink the mark."
        )

    word_h, tag_h, rule_gap, url_h = 132, 62, 34, 34
    block_h = word_h + tag_h + rule_gap + url_h

    card.paste(mark, (left, (HEIGHT - mark_h) // 2), mark)

    x = left + mark_w + gap
    y = (HEIGHT - block_h) // 2

    cursor = x
    for text, colour in WORDMARK:
        # anchor="lt" pins glyphs to a predictable top edge; Pillow's default
        # origin is the ascender, which differs between the three faces.
        draw.text((cursor, y), text, font=f_word, fill=colour, anchor="lt")
        cursor += round(draw.textlength(text, font=f_word))

    draw.text((x, y + word_h), TAGLINE, font=f_tag, fill=TEXT, anchor="lt")

    rule_y = y + word_h + tag_h + rule_gap // 2
    draw.line([(x, rule_y), (x + text_w, rule_y)], fill=BORDER, width=2)

    draw.text((x, y + word_h + tag_h + rule_gap), URL, font=f_url, fill=MUTED, anchor="lt")

    card.save(OUT, "PNG", optimize=True)
    print(f"{OUT.relative_to(REPO_ROOT)}  {WIDTH}x{HEIGHT}  {OUT.stat().st_size // 1024} KB")
    print(f"margins: {left}px each side · mark {mark_w}x{mark_h} · text block {text_w}px")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        render(Path(tmp))


if __name__ == "__main__":
    main()
