# Seeded dashboard screenshots

`make shots` renders the terminal dashboard from a YAML fixture into
`docs/images/dashboard/` (`<name>.svg` for the docs, `<name>.png` for videos).
No Incus, no state service, no containers: the real `DashboardApp` runs headless
against the test suite's `FakeStateClient` (`tests/dashboard_pilot.py`, imported,
never copied) and Textual's pilot presses the keys.

```bash
make shots                                   # every fixture under fixtures/
uv run python -m website.shots website/shots/fixtures/docs.yaml --out /tmp/shots --no-png
```

SVG colours come from the app's own ANSI theme (`app.ansi_theme`), not the
terminal's palette, with no webfont from a CDN. PNGs are rasterised by the host
Chrome (`/opt/google/chrome`); ImageMagick drops the SVG's unfilled strokes.

## Fixture format

`fixtures/docs.yaml` is the reference. Top level keys: `size: [cols, rows]`,
`now`, `view` (`columns`, `show_details`, …: the dashboard's saved view),
`repos`, `outbox`, `accounts`, `steps`. An unknown key anywhere fails with its
name (`website/shots/fixture.py`).

- **`repos`** — `prefix` plus any `RepoGroup` field, and `containers`. A
  container is `name` (short; the repo prefix is added) plus any
  `ContainerInfo` field, built on its defaults: `state`, `base_branch`,
  `pr_number`, `cpu_percent`, `memory_usage`, `created_at`, … Nested records
  use their own field names: `git_status` (ahead/behind/merge cells),
  `agent_status` (`state: waiting` is the ◆), `activity` (the DOING column),
  `grant` (makes an orchestrator) and `child: {orchestrator: <short name>,
  state: working|done|imported}` (a child, drawn as a tree under it).
- **`outbox`** — `<full container name>: [proposals]`, each `id`, `kind`
  (`pr`/`issue`), `repo`, `target`, `title`, `body`; served as the outbox
  listing the dashboard asks for.
- **`accounts`** — rows of the accounts overlay: `agent`, `group`, `account`,
  `state` (`live`/`parked`/`empty`), `repos`, `containers`.
- **`steps`** — in order: `key: j`, `keys: [escape, escape]`,
  `select: <container>` (put the cursor on it), `menu: <verb>` (open its menu and
  choose that leaf, e.g. `outbox browse`) and `shot: <name>` (write
  `<name>.svg`). The harness resizes to `size` and presses `O` (optimise column
  widths) first.

Everything that would shell out is answered from the fixture or stubbed at the
existing seams (`accounts.run_cli_quiet`, the action runner, the sent-log reader).

## Adding a shot or a fixture

1. Add steps (and any data they need) to a fixture, or copy `docs.yaml`.
2. `make shots`, then look at the PNGs.
3. Commit the images. `tests/test_shots_harness.py` renders every fixture here
   in the normal suite (no pixel comparison), so a broken fixture fails CI.

Keep the world fictional: invented repos, branches and accounts only
(`example.com` addresses).
