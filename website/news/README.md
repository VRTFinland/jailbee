# Writing JailBee news

This is the editorial source for <https://jailbee.gisgro.io/news/>. Add an
English-language Markdown article at `website/news/posts/YYYY-MM-DD-slug.md`:

```markdown
---
title: JailBee 1.6 is here
date: 2026-10-01
summary: A short, plain-text description for the archive, RSS, and link previews.
image: /assets/news/jailbee-1-6.jpg
image_alt: The JailBee logo beside the 1.6 release notes
---

The opening paragraph gives the reader context without repeating the summary.

## What's new

- Describe the user-visible change.
- Link to [installation](/docs/installation/) when relevant.
```

`title`, `date` and `summary` are required. Use a real ISO calendar date that
matches the filename prefix. The date and the `slug` part of the filename determine
the permanent `/news/YYYY/MM/DD/<slug>/` URL; keep the slug lowercase, ASCII,
and hyphen-separated, and never use `page`. Changing either later breaks
published links. The build also generates `/news/YYYY/`, `/news/YYYY/MM/` and
`/news/YYYY/MM/DD/` archives for every period that has a post. A date does **not** schedule a
post: merging it to `main` makes it public on the next Pages deployment.

The feature image is optional. If present, store the file under
`website/assets/news/` and set `image` to its site-root-relative `/assets/...`
URL and `image_alt` to meaningful alternative text. Use a landscape image of
exactly 1200×630 pixels, saved as a JPEG under ~300 KB: some social
crawlers silently drop a larger file and fall back to a text-only preview. The
same image appears in the article and archive. Without one, the site's default sharing card is used.
Inline Markdown images also use local, existing `/assets/...` paths; never
embed images from a third-party CDN. Do not use raw HTML, and start section
headings with `##`: the page supplies its own title. Normal external links
are fine; internal links should be root-relative (`/docs/`, `/news/`).

Run from the repository root:

```bash
make site
make site-check
uv run pytest tests/test_news_content.py tests/test_news_site.py tests/test_website.py
uv run python -m http.server -d _site 8099
```

Open <http://localhost:8099/news/> to review the index and the article,
including its feature image, narrow-screen layout, and link preview metadata.
`make site` fails with the article path if metadata or a local image is invalid.
The build generates the archive pages, `/news/feed.xml`, and sitemap entries;
do not edit `_site/` directly. Articles, templates and this guide under
`website/news/` are build inputs and are not copied into the published site.
