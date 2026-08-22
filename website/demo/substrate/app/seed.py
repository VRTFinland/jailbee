"""Deterministic seed rows, so every take of every video shows the same
data. Idempotent: running it twice does not duplicate rows.
"""

from __future__ import annotations

from app.db import add_item, init_db, list_items

ITEMS = ["hull inspection", "propeller polish", "anode check"]


def main() -> None:
    init_db()
    existing = {item["name"] for item in list_items()}
    for name in ITEMS:
        if name not in existing:
            add_item(name)
            # Update the snapshot as we go. Without this, a name appearing
            # twice in ITEMS would be inserted twice on a fresh database, so
            # the idempotence claim above would hold only because today's
            # list happens to have no duplicates. Note this does not make two
            # *concurrent* runs safe -- both would snapshot an empty set --
            # and there is deliberately no UNIQUE index on items.name, since
            # a viewer posting the same item twice should get a row, not a
            # 500.
            existing.add(name)


if __name__ == "__main__":
    main()
