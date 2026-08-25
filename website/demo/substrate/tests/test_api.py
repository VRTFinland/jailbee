"""API tests for the demo app.

Deliberately does NOT test /health: adding that endpoint is the task the agent
performs on camera in video A, and a test that already expects it would make
the agent's change look pre-arranged.

Every test runs against a fresh database — see conftest.py, which points
$DEMO_DB at tmp_path automatically.
"""

from fastapi.testclient import TestClient

from app.db import add_item, init_db
from app.main import app


def test_the_lifespan_creates_the_schema_on_a_database_nobody_touched() -> None:
    """No init_db() call here, on purpose.

    The app has to stand up on a database that has never been opened, because
    that is what `uvicorn app.main:app` does when the autostart server step
    runs. With the lifespan handler removed this fails with
    "no such table: items" — the 500 a viewer would see on camera if the
    schema were only ever created as a side effect of the seed step.
    """
    with TestClient(app) as client:
        response = client.get("/items")
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_added_items_come_back_in_insertion_order() -> None:
    """Insertion order, not alphabetical order.

    The names matter: "propeller polish" sorts *after* "anode check", so an
    `ORDER BY name` — or no ORDER BY at all once rows are not scanned in
    rowid order — produces the opposite sequence and fails. An earlier
    version of this test used two names whose alphabetical and insertion
    order coincided, and `ORDER BY name` passed it.
    """
    init_db()
    add_item("propeller polish")
    add_item("anode check")
    with TestClient(app) as client:
        response = client.get("/items")
    names = [item["name"] for item in response.json()["items"]]
    assert names == ["propeller polish", "anode check"]


def test_posting_an_item_returns_its_id_and_makes_it_readable() -> None:
    """Asserts the id, not just the status code.

    `add_item` returns `int(cursor.lastrowid or 0)`, which turns a missing
    lastrowid into id 0 — an id no row can hold. Checking only the 201 leaves
    that undetectable, and a `return 0` in add_item passed the earlier version
    of this test.
    """
    init_db()
    with TestClient(app) as client:
        created = client.post("/items", json={"name": "anode check"})
        assert created.status_code == 201
        assert created.json() == {"id": 1}
        listed = client.get("/items").json()["items"]
    assert listed == [{"id": 1, "name": "anode check"}]
