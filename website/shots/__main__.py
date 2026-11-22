"""``python -m website.shots`` — regenerate every fixture's images (``make shots``)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from website.shots.fixture import load_world
from website.shots.render import render_world, svg_to_png

HERE = Path(__file__).parent
ROOT = HERE.parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m website.shots")
    parser.add_argument("fixtures", nargs="*", type=Path, help="fixture files (default: all)")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images" / "dashboard")
    parser.add_argument("--no-png", action="store_true", help="write SVG only")
    args = parser.parse_args(argv)
    fixtures = args.fixtures or sorted((HERE / "fixtures").glob("*.yaml"))
    for fixture in fixtures:
        for svg in render_world(load_world(fixture), args.out):
            print(svg.relative_to(ROOT) if svg.is_relative_to(ROOT) else svg)
            if not args.no_png:
                svg_to_png(svg, svg.with_suffix(".png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
