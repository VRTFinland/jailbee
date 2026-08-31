"""The demo app. Serves on port 8080 -- the port the site's own problem
statement names ("Port 8080 is taken", website/index.html).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.db import add_item, init_db, list_items


class NewItem(BaseModel):
    name: str


# A lifespan handler, not @app.on_event("startup"): on_event is deprecated in
# the FastAPI versions this pins and would print a DeprecationWarning into
# every recorded pytest run. Schema creation has to happen at startup rather
# than at import so the tests can point $DEMO_DB at tmp_path first.
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="jailbee-demo", lifespan=lifespan)


@app.get("/items")
def get_items() -> dict[str, list[dict[str, str | int]]]:
    return {"items": list_items()}


@app.post("/items", status_code=201)
def post_item(item: NewItem) -> dict[str, int]:
    return {"id": add_item(item.name)}
