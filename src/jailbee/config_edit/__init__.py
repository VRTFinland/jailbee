"""The config editor: schema introspection, layer resolution, editor state.

Three rings, so that most of it can be unit-tested without a terminal or a
real config file — the split `dashboard_settings.py` uses on the Rich side:

* **pure core** — `schema` (what fields exist), `state` (what is on screen and
  what is staged), `values` (text in, value out), `render` (state in,
  fragments out). Data in, data out; no file, no terminal.
* **filesystem edge** — `layers` (reads both raw layers, and runs the real
  loader over a staged one) and `save` (renders a layer, diffs it, writes it
  behind a backup).
* **driver** — `app`, the one `Application`: its key bindings, its mutable
  session, and nothing else.

Only `app` needs a terminal, and only `layers` and `save` touch the disk.
"""
