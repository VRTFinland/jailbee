"""Generated news pages and syndication, built without a server or network."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.news_site import build

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def site(tmp_path: Path) -> tuple[Path, Path]:
    website = tmp_path / "website"
    output = tmp_path / "_site"
    templates = website / "news" / "templates"
    templates.parent.mkdir(parents=True)
    shutil.copytree(REPO / "website" / "news" / "templates", templates)
    assets = website / "assets"
    assets.mkdir()
    (assets / "style.css").write_text("body { color: red; }")
    (assets / "news.css").write_text("article { max-width: 60ch; }")
    (assets / "feature.png").write_bytes(b"image")
    shutil.copy(REPO / "website" / "index.html", website / "index.html")
    (website / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://jailbee.gisgro.io/</loc></url></urlset>"
    )
    return website, output


def _add(website: Path, filename: str, metadata: str, body: str = "Story.\n") -> None:
    posts = website / "news" / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    (posts / filename).write_text(f"---\n{metadata}---\n{body}")


META = "title: JailBee 1.5\ndate: 2026-09-28\nsummary: Release notes\n"


def test_no_posts_renders_a_readable_empty_index(site: tuple[Path, Path]) -> None:
    website, output = site
    build(website, output)

    html = (output / "news" / "index.html").read_text()
    assert "No news yet" in html
    assert 'href="/"' in html
    assert "JailBee" in html


def test_article_has_body_brand_and_canonical_url(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        META,
        "## What changed\n\n- Faster start\n\n[Read docs](/docs/)\n",
    )
    build(website, output)

    html = (output / "news" / "2026" / "09" / "28" / "release" / "index.html").read_text()
    assert html.count("<h1") == 1
    assert "<h1>JailBee 1.5</h1>" in html
    assert "<h2>What changed</h2>" in html
    assert "<li>Faster start</li>" in html
    assert '<a href="/docs/">Read docs</a>' in html
    assert '<link rel="canonical" href="https://jailbee.gisgro.io/news/2026/09/28/release/"' in html
    assert "2026-09-28" in html
    assert "Release notes" in html
    assert "topbar" in html and "footer" in html
    assert "/assets/style.css?v=" in html and "/assets/news.css?v=" in html
    assert "https://jailbee.gisgro.io/assets/img/jailbee-og.png" in html


def test_every_news_page_carries_the_home_pages_top_bar(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META)
    build(website, output)

    home = (website / "index.html").read_text()
    version = re.search(r'class="topbar__version">(v[^<]*)<', home)
    assert version is not None
    for page in (
        output / "news" / "index.html",
        output / "news" / "2026" / "09" / "28" / "release" / "index.html",
    ):
        html = page.read_text()
        assert html.count('<header class="topbar">') == 1
        # The icons, the source box and the release number all come along.
        assert html.count("<svg") == home[home.index('<header class="topbar">') :].split(
            "</header>"
        )[0].count("<svg")
        assert f'class="topbar__version">{version.group(1)}<' in html
        assert 'href="https://github.com/VRTFinland/jailbee"' in html
        # Links resolve from /news/YYYY/MM/DD/<slug>/, so none may stay relative.
        header = html[html.index('<header class="topbar">') : html.index("</header>")]
        assert 'href="/"' in header and 'href="/news/"' in header and 'href="/docs/"' in header
        assert 'src="/assets/img/jailbee-mark.png"' in header
        assert 'href="news/"' not in header and 'src="assets/' not in header
        assert "<!--" not in header


def test_the_newest_title_is_in_the_bar_of_every_page_and_the_home_page(
    site: tuple[Path, Path],
) -> None:
    website, output = site
    _add(website, "2026-09-01-old.md", "title: Older\ndate: 2026-09-01\nsummary: s\n")
    _add(website, "2026-10-02-new.md", "title: Newer <1>\ndate: 2026-10-02\nsummary: s\n")
    build(website, output)

    article = (output / "news" / "2026" / "09" / "01" / "old" / "index.html").read_text()
    assert 'class="topbar__latest" href="/news/2026/10/02/new/"' in article
    assert "Newer &lt;1&gt;" in article and "Older</a>" not in article.split("</header>")[0]
    home = (output / "index.html").read_text()
    assert 'class="topbar__latest" href="news/2026/10/02/new/"' in home
    assert home.count("topbar__latest") == 1
    # The committed page stays chip-free; the chip is a build product.
    assert "topbar__latest" not in (website / "index.html").read_text()


def _home_news(home: str) -> str:
    match = re.search(r'<section id="news">.*?</section>', home, re.DOTALL)
    assert match is not None, "the home page has no news section"
    return match.group(0)


def test_the_home_page_lists_the_three_newest_posts(site: tuple[Path, Path]) -> None:
    website, output = site
    for day in ("01", "02", "03", "04"):
        _add(
            website,
            f"2026-09-{day}-post-{day}.md",
            f"title: Post {day}\ndate: 2026-09-{day}\nsummary: Summary {day}\n"
            "image: /assets/feature.png\nimage_alt: Feature <alt>\n",
        )
    build(website, output)

    section = _home_news((output / "index.html").read_text())
    titles = re.findall(r'<h3><a href="([^"]+)">([^<]+)</a></h3>', section)
    assert titles == [
        ("news/2026/09/04/post-04/", "Post 04"),
        ("news/2026/09/03/post-03/", "Post 03"),
        ("news/2026/09/02/post-02/", "Post 02"),
    ]
    assert "Summary 04" in section and "Summary 01" not in section
    assert '<time datetime="2026-09-04">2026-09-04</time>' in section
    # Relative like the rest of the home page, and escaped.
    assert 'src="assets/feature.png"' in section and 'alt="Feature &lt;alt&gt;"' in section
    assert 'href="news/"' in section and 'href="news/feed.xml"' in section
    # It sits last, after the docs and before the footer.
    home = (output / "index.html").read_text()
    assert home.index('id="docs"') < home.index('id="news"') < home.index("<footer>")


def test_a_post_without_an_image_gets_a_card_without_one(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META)
    build(website, output)

    section = _home_news((output / "index.html").read_text())
    assert "JailBee 1.5" in section and "<img" not in section


def test_the_committed_home_page_carries_no_news_section(site: tuple[Path, Path]) -> None:
    website, output = site
    build(website, output)

    # The section is a build product, like the chip; with no posts the home
    # page is left as the source has it, which shows none.
    assert 'id="news"' not in (website / "index.html").read_text()
    assert not (output / "index.html").exists()


def test_a_home_page_without_the_news_marker_fails_the_build(site: tuple[Path, Path]) -> None:
    website, output = site
    index = website / "index.html"
    index.write_text(index.read_text().replace("<!-- news:latest -->", ""))
    _add(website, "2026-09-28-release.md", META)

    with pytest.raises(SystemExit, match="news:latest"):
        build(website, output)


def test_link_previews_get_a_sized_image_a_plain_title_and_the_right_type(
    site: tuple[Path, Path],
) -> None:
    website, output = site
    # A real 1200x630 PNG header is all the size reader looks at.
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1200).to_bytes(4, "big")
        + (630).to_bytes(4, "big")
    )
    (website / "assets" / "feature.png").write_bytes(png)
    _add(
        website,
        "2026-09-28-release.md",
        META + "image: /assets/feature.png\nimage_alt: A hive\n",
    )
    _add(website, "2026-09-29-plain.md", "title: Plain\ndate: 2026-09-29\nsummary: s\n")
    build(website, output)

    article = (output / "news" / "2026" / "09" / "28" / "release" / "index.html").read_text()
    assert '<meta property="og:title" content="JailBee 1.5" />' in article
    assert '<meta property="og:type" content="article" />' in article
    assert '<meta property="article:published_time" content="2026-09-28" />' in article
    assert 'property="og:image" content="https://jailbee.gisgro.io/assets/feature.png"' in article
    assert 'property="og:image:width" content="1200"' in article
    assert 'property="og:image:height" content="630"' in article
    assert 'property="og:image:alt" content="A hive"' in article

    plain = (output / "news" / "2026" / "09" / "29" / "plain" / "index.html").read_text()
    assert "/assets/img/jailbee-og.png" in plain  # the default card
    assert 'property="og:image:alt" content="JailBee' in plain
    assert "og:image:width" not in plain  # the fixture ships no default image to measure

    index = (output / "news" / "index.html").read_text()
    assert '<meta property="og:type" content="website" />' in index
    assert "article:published_time" not in index


def _png(width: int, height: int) -> bytes:
    """A PNG header: all the size reader looks at."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _article_body(output: Path) -> str:
    html = (output / "news" / "2026" / "09" / "28" / "release" / "index.html").read_text()
    return html[html.index('class="news-article__body"') :]


def test_an_image_on_its_own_line_becomes_a_sized_figure_with_its_title_as_caption(
    site: tuple[Path, Path],
) -> None:
    website, output = site
    (website / "assets" / "shot.png").write_bytes(_png(496, 378))
    _add(
        website,
        "2026-09-28-release.md",
        META,
        'Before.\n\n![The <AI> column](/assets/shot.png "Agents & their state")\n\nAfter.\n',
    )
    build(website, output)

    body = _article_body(output)
    figure = re.search(r'<figure class="news-figure">.*?</figure>', body, re.DOTALL)
    assert figure is not None
    html = figure.group(0)
    # Its natural size, so the browser never scales a screenshot up.
    assert 'width="496"' in html and 'height="378"' in html
    assert 'src="/assets/shot.png"' in html and 'alt="The &lt;AI&gt; column"' in html
    assert "<figcaption>Agents &amp; their state</figcaption>" in html
    assert "title=" not in html
    assert "<p><img" not in body


def test_a_figure_without_a_title_has_no_caption(site: tuple[Path, Path]) -> None:
    website, output = site
    (website / "assets" / "shot.png").write_bytes(_png(10, 10))
    _add(website, "2026-09-28-release.md", META, "![Alt](/assets/shot.png)\n")
    build(website, output)

    body = _article_body(output)
    assert '<figure class="news-figure">' in body and "<figcaption" not in body


def test_an_image_inside_a_sentence_stays_inline(site: tuple[Path, Path]) -> None:
    website, output = site
    (website / "assets" / "shot.png").write_bytes(_png(10, 10))
    _add(website, "2026-09-28-release.md", META, "Look ![icon](/assets/shot.png) here.\n")
    build(website, output)

    body = _article_body(output)
    assert "<figure" not in body and '<img alt="icon" src="/assets/shot.png"' in body


VIDEO = "rjkssscOuBk"


def _thumbnail(website: Path, video_id: str = VIDEO) -> None:
    videos = website / "assets" / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    (videos / f"{video_id}.jpg").write_bytes(_png(1280, 720))


def _article(output: Path) -> str:
    return (output / "news" / "2026" / "09" / "28" / "release" / "index.html").read_text()


@pytest.mark.parametrize(
    "line",
    [
        f"https://youtu.be/{VIDEO}",
        f"<https://www.youtube.com/watch?v={VIDEO}>",
        f"https://youtube.com/embed/{VIDEO}",
    ],
)
def test_a_youtube_url_on_its_own_line_becomes_a_click_to_load_player(
    site: tuple[Path, Path], line: str
) -> None:
    website, output = site
    _thumbnail(website)
    (website / "assets" / "lite-yt.js").write_text("// player")
    _add(website, "2026-09-28-release.md", META, f"Intro.\n\n{line}\n\nAfter.\n")
    build(website, output)

    html = _article(output)
    figure = r'<figure class="yt yt--video" data-yt-id="([^"]+)">.*?</figure>'
    player = re.search(figure, html, re.S)
    assert player is not None and player.group(1) == VIDEO
    block = player.group(0)
    # Nothing from YouTube until the click: a local thumbnail, a button, and a
    # plain link for readers without JavaScript.
    assert f'src="/assets/videos/{VIDEO}.jpg"' in block and 'width="1280"' in block
    assert '<button type="button" class="yt__play" aria-label="Play video: JailBee 1.5">' in block
    assert f'<a class="yt__link" href="https://www.youtube.com/watch?v={VIDEO}"' in block
    assert "youtube.com/embed" not in block and "youtube-nocookie" not in block
    assert re.search(r'<script src="/assets/lite-yt\.js\?v=[0-9a-f]{8}" defer></script>', html)


def test_a_short_gets_the_portrait_player(site: tuple[Path, Path]) -> None:
    website, output = site
    _thumbnail(website, "4HqNJCPtfws")
    (website / "assets" / "lite-yt.js").write_text("// player")
    _add(website, "2026-09-28-release.md", META, "https://youtube.com/shorts/4HqNJCPtfws\n")
    build(website, output)

    assert '<figure class="yt yt--short" data-yt-id="4HqNJCPtfws">' in _article(output)


def test_a_page_without_a_video_loads_no_script(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        META,
        f"See https://youtu.be/{VIDEO} for more.\n\n```\nhttps://youtu.be/{VIDEO}\n```\n",
    )
    build(website, output)

    html = _article(output)
    assert "<script" not in html and 'class="yt' not in html


def test_a_video_without_its_thumbnail_fails_the_build_naming_the_file(
    site: tuple[Path, Path],
) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META, f"https://youtu.be/{VIDEO}\n")

    with pytest.raises(ValueError, match=f"assets/videos/{VIDEO}.jpg"):
        build(website, output)


def test_feature_image_and_metadata_are_escaped(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        'title: "A & <B>"\ndate: 2026-09-28\nsummary: "It has \\"quotes\\" & news"\n'
        'image: /assets/feature.png\nimage_alt: "An <image> & a bee"\n',
    )
    build(website, output)

    article = (output / "news" / "2026" / "09" / "28" / "release" / "index.html").read_text()
    archive = (output / "news" / "index.html").read_text()
    for page in (article, archive):
        assert "A &amp; &lt;B&gt;" in page
        assert "An &lt;image&gt; &amp; a bee" in page
        assert 'src="/assets/feature.png"' in page
    assert "https://jailbee.gisgro.io/assets/feature.png" in article
    assert "It has &#34;quotes&#34; &amp; news" in article


@pytest.mark.parametrize(
    "body",
    [
        "![Remote](https://cdn.example.com/image.png)\n",
        "![Remote][picture]\n\n[picture]: https://cdn.example.com/image.png\n",
        "![An [illustrated] update](/assets/missing.png)\n",
        "![Missing][picture]\n\n[picture]: /assets/missing.png\n",
        '<img src="https://cdn.example.com/image.png">\n',
        '<pre><code><script src="https://cdn.example.com/x.js"></script></code></pre>\n',
        "![Missing](/assets/missing.png)\n",
        "# Another title\n",
        "Another title\n=============\n",
        "> # Quoted title\n",
    ],
)
def test_bad_body_fails_with_article_path(site: tuple[Path, Path], body: str) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META, body)
    with pytest.raises(ValueError, match=r"2026-09-28-release\.md"):
        build(website, output)


def test_code_examples_are_not_treated_as_raw_html_or_headings(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        META,
        '```html\n<img src="https://example.com/demo.png">\n```\n\n'
        "```sh\n# this is a shell comment\n```\n\n"
        '`<img src="example">` is only an example.\n',
    )
    build(website, output)
    html = (output / "news" / "2026" / "09" / "28" / "release" / "index.html").read_text()
    assert html.count("<h1") == 1
    assert "&lt;img src=" in html
    assert "# this is a shell comment" in html


@pytest.mark.parametrize("count,expected_pages", [(0, 1), (10, 1), (11, 2), (21, 3)])
def test_pagination_obeys_ten_posts_per_page(
    site: tuple[Path, Path], count: int, expected_pages: int
) -> None:
    website, output = site
    for number in range(count):
        _add(website, f"2026-09-28-post-{number:02}.md", META)
    build(website, output)

    pages = [output / "news" / "index.html"] + [
        output / "news" / "page" / str(n) / "index.html" for n in range(2, expected_pages + 1)
    ]
    assert all(page.is_file() for page in pages)
    assert [page.read_text().count('class="news-card"') for page in pages] == (
        [min(count, 10)] + [min(count - 10 * (n - 1), 10) for n in range(2, expected_pages + 1)]
    )
    assert not (output / "news" / "page" / str(expected_pages + 1)).exists()
    assert '<link rel="canonical" href="https://jailbee.gisgro.io/news/"' in pages[0].read_text()
    if count > 10:
        assert 'href="/news/page/2/"' in pages[0].read_text()
        assert (
            '<link rel="canonical" href="https://jailbee.gisgro.io/news/page/2/"'
            in pages[1].read_text()
        )
        assert 'href="/news/"' in pages[1].read_text()


def test_rss_is_valid_ordered_and_discoverable(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-new.md",
        'title: "New & <important>"\ndate: 2026-09-28\nsummary: "A & B"\n',
    )
    _add(website, "2026-09-27-old.md", META.replace("2026-09-28", "2026-09-27"))
    build(website, output)

    tree = ET.parse(output / "news" / "feed.xml")
    items = tree.findall("./channel/item")
    assert [item.findtext("title") for item in items] == ["New & <important>", "JailBee 1.5"]
    assert [item.findtext("link") for item in items] == [
        "https://jailbee.gisgro.io/news/2026/09/28/new/",
        "https://jailbee.gisgro.io/news/2026/09/27/old/",
    ]
    assert items[0].findtext("guid") == "https://jailbee.gisgro.io/news/2026/09/28/new/"
    assert items[0].findtext("description") == "A & B"
    assert items[0].findtext("pubDate") == "Mon, 28 Sep 2026 00:00:00 GMT"
    html = (output / "news" / "index.html").read_text()
    assert 'type="application/rss+xml"' in html
    assert 'href="/news/feed.xml"' in html


def _sitemap_urls(output: Path) -> set[str]:
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(output / "sitemap.xml")
    return {loc.text or "" for loc in tree.findall(".//s:loc", namespace)}


def test_sitemap_contains_only_current_news_pages(site: tuple[Path, Path]) -> None:
    website, output = site
    for number in range(11):
        _add(website, f"2026-09-28-post-{number:02}.md", META)
    build(website, output)
    assert _sitemap_urls(output) == {
        "https://jailbee.gisgro.io/",
        "https://jailbee.gisgro.io/news/",
        "https://jailbee.gisgro.io/news/page/2/",
        *(f"https://jailbee.gisgro.io/news/2026/09/28/post-{number:02}/" for number in range(11)),
    }

    (output / "index.html").write_text("Home")
    docs = output / "docs" / "index.html"
    docs.parent.mkdir()
    docs.write_text("Docs")
    for number in range(1, 11):
        (website / "news" / "posts" / f"2026-09-28-post-{number:02}.md").unlink()
    build(website, output)
    # The home page is rewritten from its source, with the newest title in the bar;
    # the docs are never touched.
    assert "topbar__latest" in (output / "index.html").read_text()
    assert docs.read_text() == "Docs"
    assert not (output / "news" / "page" / "2").exists()
    assert not (output / "news" / "2026" / "09" / "28" / "post-01").exists()
    assert _sitemap_urls(output) == {
        "https://jailbee.gisgro.io/",
        "https://jailbee.gisgro.io/news/",
        "https://jailbee.gisgro.io/news/2026/09/28/post-00/",
    }


def test_builder_does_not_copy_editorial_sources_into_output(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META)
    (website / "news" / "README.md").write_text("Editing instructions")
    build(website, output)
    assert sorted(p.relative_to(output) for p in output.rglob("*.md")) == []
    assert not (output / "news" / "templates").exists()


def test_cli_returns_path_specific_diagnostic_on_invalid_article(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-bad.md", "title: Missing date\nsummary: Summary\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.news_site",
            "--website-dir",
            str(website),
            "--site-dir",
            str(output),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "2026-09-28-bad.md" in result.stderr
    assert "date" in result.stderr


def test_year_month_and_day_archives_list_only_their_posts(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-10-02-a.md", "title: Alpha\ndate: 2026-10-02\nsummary: s\n")
    _add(website, "2026-10-15-b.md", "title: Beta\ndate: 2026-10-15\nsummary: s\n")
    _add(website, "2025-12-31-c.md", "title: Gamma\ndate: 2025-12-31\nsummary: s\n")
    build(website, output)

    def titles(*parts: str) -> list[str]:
        html = (output.joinpath("news", *parts, "index.html")).read_text()
        return re.findall(r"<h2><a [^>]*>([^<]*)</a></h2>", html)

    assert titles("2026") == ["Beta", "Alpha"]
    assert titles("2026", "10") == ["Beta", "Alpha"]
    assert titles("2026", "10", "02") == ["Alpha"]
    assert titles("2025") == ["Gamma"]
    assert not (output / "news" / "2026" / "11").exists()
    month = (output / "news" / "2026" / "10" / "index.html").read_text()
    assert "<h1>News, October 2026</h1>" in month
    assert '<link rel="canonical" href="https://jailbee.gisgro.io/news/2026/10/"' in month
    article = (output / "news" / "2026" / "10" / "02" / "a" / "index.html").read_text()
    for href in ("/news/2026/", "/news/2026/10/", "/news/2026/10/02/"):
        assert f'href="{href}"' in article


def test_period_archives_paginate_and_stay_out_of_the_sitemap(site: tuple[Path, Path]) -> None:
    website, output = site
    for number in range(11):
        _add(website, f"2026-09-28-post-{number:02}.md", META)
    build(website, output)

    page = output / "news" / "2026" / "09" / "page" / "2" / "index.html"
    assert page.read_text().count('class="news-card"') == 1
    assert 'href="/news/2026/09/"' in page.read_text()
    sitemap = (output / "sitemap.xml").read_text()
    assert "/news/2026/09/<" not in sitemap and "/news/2026/<" not in sitemap
