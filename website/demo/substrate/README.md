# jailbee-demo

A deliberately small application, used to demonstrate
[JailBee](https://jailbee.gisgro.io) in its workflow videos.

It is a FastAPI service with a per-container SQLite store, serving **port
8080** — the port JailBee's own documentation uses when it describes what
goes wrong when two branches share one machine.

## Running it

```bash
uv sync
uv run python app/seed.py                 # deterministic demo rows
uv run uvicorn app.main:app --reload --port 8080
```

```bash
curl localhost:8080/items
curl -X POST localhost:8080/items -H 'content-type: application/json' \
     -d '{"name": "anode check"}'
```

## Tests

```bash
uv run pytest
```

## In a JailBee container

`.jailbee/config.yaml` provisions everything on `jailbee new`: dependencies
sync, the database is seeded, the dev server comes up in a tmux window, and an
agent gets its own window beside it.

```bash
jailbee new feat/my-change
jailbee tmux feat-my-change
```
