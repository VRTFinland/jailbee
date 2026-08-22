"""Test-wide isolation for the demo database.

``app/db.py`` falls back to ``<repo>/var/app.db`` when ``$DEMO_DB`` is unset —
the real, gitignored database holding the rows that appear on camera. A test
that forgot to set the variable would write into it, the next recording would
show duplicated items, and the failure would point at the demo data rather
than at the missing line.

An autouse fixture makes that impossible instead of relying on every test
author remembering it. This matters more here than in most projects: an agent
adds an endpoint *and its test* on camera, and it has no way to know about a
convention that lives only in the existing tests.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DEMO_DB", str(tmp_path / "app.db"))
    yield
