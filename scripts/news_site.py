"""Build the branded, static news section from repository Markdown."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, time
from email.utils import format_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from scripts.news_content import Post, asset_path, load_posts

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"
SITE = REPO / "_site"
URL = "https://jailbee.gisgro.io"
DEFAULT_IMAGE = "/assets/img/jailbee-og.png"
DEFAULT_IMAGE_ALT = "JailBee: one container per branch"
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_ARTICLE_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}


class _ArticleElements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1_count = 0
        self.images: list[str] = []
        self.invalid_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ARTICLE_TAGS or any(
            name.startswith("on") or name == "style" for name, _ in attrs
        ):
            self.invalid_tag = tag
        if tag == "h1":
            self.h1_count += 1
        if tag == "img":
            self.images.append(dict(attrs).get("src") or "")


def _content(post: Post, assets_dir: Path) -> str:
    source = Path(f"{post.date}-{post.slug}.md")
    parser = markdown.Markdown(extensions=["fenced_code", "tables"])
    html = parser.convert(post.body_md)
    # Python-Markdown stashes fenced code as a generated <pre><code> block;
    # reject other raw HTML, without mistaking an example inside a fence for it.
    for raw in parser.htmlStash.rawHtmlBlocks:
        if not raw.startswith("<pre><code"):
            raise ValueError(f"{source}: raw HTML is not allowed in article Markdown")
    elements = _ArticleElements()
    elements.feed(html)
    if elements.invalid_tag:
        raise ValueError(f"{source}: unsupported HTML in article: {elements.invalid_tag}")
    if elements.h1_count:
        raise ValueError(f"{source}: use ## for article headings, not a second # title")
    for path in elements.images:
        asset_path(path, assets_dir, source)
    return html


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def _archive_url(base: str, page: int) -> str:
    """URL of one page of the archive at `base` ("/news/", "/news/2026/10/", ...)."""
    return base if page == 1 else f"{base}page/{page}/"


def _periods(posts: list[Post]) -> dict[str, tuple[str, list[Post]]]:
    """The year, month and day archives that have posts: base URL -> (label, posts)."""
    periods: dict[str, tuple[str, list[Post]]] = {}
    for post in posts:
        d = post.date
        for base, label in (
            (f"/news/{d:%Y}/", f"{d:%Y}"),
            (f"/news/{d:%Y/%m}/", f"{d:%B %Y}"),
            (f"/news/{d:%Y/%m/%d}/", f"{d.day} {d:%B %Y}"),
        ):
            periods.setdefault(base, (label, []))[1].append(post)
    return periods


def _feed(posts: list[Post], path: Path) -> None:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    for tag, value in (
        ("title", "JailBee News"),
        ("link", f"{URL}/news/"),
        ("description", "JailBee releases and announcements."),
    ):
        ET.SubElement(channel, tag).text = value
    for post in posts:
        item = ET.SubElement(channel, "item")
        canonical = f"{URL}{post.path}"
        for tag, value in (
            ("title", post.title),
            ("link", canonical),
            ("guid", canonical),
            ("description", post.summary),
            ("pubDate", format_datetime(datetime.combine(post.date, time.min, UTC), usegmt=True)),
        ):
            ET.SubElement(item, tag).text = value
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def _sitemap(website_dir: Path, site_dir: Path, urls: list[str]) -> None:
    ET.register_namespace("", _SITEMAP_NS)
    tree = ET.parse(website_dir / "sitemap.xml")
    root = tree.getroot()
    for url in urls:
        entry = ET.SubElement(root, f"{{{_SITEMAP_NS}}}url")
        ET.SubElement(entry, f"{{{_SITEMAP_NS}}}loc").text = f"{URL}{url}"
    tree.write(site_dir / "sitemap.xml", encoding="utf-8", xml_declaration=True)


_NEWS_LINK = '<a class="topbar__link topbar__link--news"'


def _with_latest(header: str, post: Post | None, prefix: str) -> str:
    """Put the newest article's title in the bar, ahead of the News link.

    `prefix` is what the article's path hangs from: "" on the home page,
    "/" everywhere else. The source index.html carries no chip, so the bar
    stays valid HTML without a build and nothing needs editing per release.
    """
    if post is None or _NEWS_LINK not in header:
        return header
    chip = (
        f'<a class="topbar__latest" href="{prefix}{post.path.removeprefix("/")}">'
        f"<span>Latest</span> {escape(post.title)}</a>\n        "
    )
    return header.replace(_NEWS_LINK, chip + _NEWS_LINK, 1)


def _image_size(path: Path) -> tuple[int, int] | None:
    """Pixel size of a PNG or JPEG, read from its header; None for anything else."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                break
            marker = data[offset + 1]
            if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
                return width, height
            offset += 2 + struct.unpack(">H", data[offset + 2 : offset + 4])[0]
    return None


def _social_image(website_dir: Path, post: Post | None) -> dict[str, object]:
    """What a link preview needs to know about the page's image."""
    path = post.image if post and post.image else DEFAULT_IMAGE
    alt = post.image_alt if post and post.image else DEFAULT_IMAGE_ALT
    size = _image_size(website_dir / path.removeprefix("/"))
    return {
        "og_image": f"{URL}{path}",
        "og_image_alt": alt,
        "og_image_width": size[0] if size else None,
        "og_image_height": size[1] if size else None,
    }


def _topbar(website_dir: Path, latest: Post | None = None) -> Markup:
    """The home page's top bar, rewritten for pages that are not at the root.

    The home page owns the markup (icons, source box, release number kept in
    step by scripts/site_version.py); news pages reuse it so the two cannot
    drift apart. Its relative links become root-relative.
    """
    html = (website_dir / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<header class="topbar">.*?</header>', html, re.DOTALL)
    if match is None:
        sys.exit('news_site: website/index.html has no <header class="topbar">')
    header = re.sub(r"<!--.*?-->\s*", "", match.group(0), flags=re.DOTALL)

    def absolute(link: re.Match[str]) -> str:
        target = link.group("target")
        if re.match(r"([a-z][a-z0-9+.-]*:|/|#)", target):
            return link.group(0)
        return f'{link.group("attr")}="/{target.removeprefix("./")}"'

    header = re.sub(r'(?P<attr>href|src)="(?P<target>[^"]*)"', absolute, header)
    return Markup(_with_latest(header, latest, "/"))


_HOME_NEWS_MARKER = "<!-- news:latest -->"
HOME_NEWS_POSTS = 3


def _home(website_dir: Path, site_dir: Path, posts: list[Post], env: Environment) -> None:
    """Write the home page with the newest title in its top bar and the newest
    posts in its news section.

    Both are build products: the source index.html carries only the marker
    comment, so it stays a complete page without a build and nothing needs
    editing per release.
    """
    if not posts:
        return
    html = (website_dir / "index.html").read_text(encoding="utf-8")
    if _HOME_NEWS_MARKER not in html:
        sys.exit(f"news_site: website/index.html has no {_HOME_NEWS_MARKER} marker")
    section = env.get_template("home_news.html").render(posts=posts[:HOME_NEWS_POSTS])
    html = html.replace(_HOME_NEWS_MARKER, section.strip(), 1)
    match = re.search(r'<header class="topbar">.*?</header>', html, re.DOTALL)
    if match is not None:
        html = html.replace(match.group(0), _with_latest(match.group(0), posts[0], ""), 1)
    (site_dir / "index.html").write_text(html, encoding="utf-8")


def build(website_dir: Path, site_dir: Path) -> None:
    posts = load_posts(website_dir / "news" / "posts", website_dir / "assets")
    env = Environment(
        loader=FileSystemLoader(website_dir / "news" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    output = site_dir / "news"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    shared = {
        "topbar": _topbar(website_dir, posts[0] if posts else None),
        "style_hash": _hash(website_dir / "assets" / "style.css"),
        "news_hash": _hash(website_dir / "assets" / "news.css"),
    }
    _home(website_dir, site_dir, posts, env)
    sitemap_urls = []

    def write_archive(base: str, label: str | None, entries: list[Post]) -> None:
        pages = [entries[start : start + 10] for start in range(0, len(entries), 10)] or [[]]
        name = "News" if label is None else f"News, {label}"
        for number, page_posts in enumerate(pages, 1):
            url = _archive_url(base, number)
            target = site_dir / url.removeprefix("/")
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.html").write_text(
                env.get_template("index.html").render(
                    posts=page_posts,
                    heading=name,
                    previous=_archive_url(base, number - 1) if number > 1 else None,
                    next=_archive_url(base, number + 1) if number < len(pages) else None,
                    canonical=f"{URL}{url}",
                    title=f"{name} — JailBee"
                    if number == 1
                    else f"{name}, page {number} — JailBee",
                    og_title="JailBee News",
                    og_type="website",
                    **_social_image(website_dir, None),
                    **shared,
                ),
                encoding="utf-8",
            )
            if label is None:
                sitemap_urls.append(url)

    write_archive("/news/", None, posts)
    for base, (label, entries) in _periods(posts).items():
        write_archive(base, label, entries)
    for post in posts:
        sitemap_urls.append(post.path)
        target = site_dir / post.path.removeprefix("/")
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(
            env.get_template("article.html").render(
                post=post,
                body_html=_content(post, website_dir / "assets"),
                canonical=f"{URL}{post.path}",
                title=f"{post.title} — JailBee",
                og_title=post.title,
                og_type="article",
                published=post.date.isoformat(),
                **_social_image(website_dir, post),
                **shared,
            ),
            encoding="utf-8",
        )
    _feed(posts, output / "feed.xml")
    _sitemap(website_dir, site_dir, sitemap_urls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--website-dir", type=Path, default=WEBSITE)
    parser.add_argument("--site-dir", type=Path, default=SITE)
    args = parser.parse_args(argv)
    try:
        build(args.website_dir, args.site_dir)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
