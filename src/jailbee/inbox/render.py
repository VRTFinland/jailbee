"""jq-compatible inbox rendering."""

import json
import re
from collections.abc import Sequence


def clean_c0c1(s: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", s)


def clean_c1(s: str) -> str:
    return re.sub(r"[\x80-\x9f]", "?", s)


def jq_tostring(v: object) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def tsv(cells: Sequence[str]) -> str:
    return "\t".join(
        s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
        for s in cells
    )


def pretty(v: object) -> str:
    return json.dumps(v, indent=2, ensure_ascii=False).replace("\x7f", "\\u007f")


def _alternative(*values: object) -> object:
    return next((v for v in values if v is not None and v is not False), None)


def ls_line(doc: dict[str, object]) -> str:
    kind = doc.get("type")
    text = jq_tostring(_alternative(doc.get("body"), doc.get("detail"), ""))[:60]
    if kind == "message":
        sha = doc.get("imported_sha")
        if sha is not None and sha is not False:
            if not isinstance(sha, str):
                raise ValueError("invalid imported sha")
            text += f" [imported {sha[:12]}]"
        if doc.get("import_error") is not None and doc.get("import_error") is not False:
            text += " [import failed]"
        reason = doc.get("on_done_reason")
        if reason is not None and reason is not False:
            text += f" [kept: {jq_tostring(reason)[:60]}]"
    cells = [
        doc.get("id"),
        kind,
        doc.get("ref" if kind == "status" else "from"),
        doc.get("state") if kind == "status" else _alternative(doc.get("kind"), ""),
        text,
    ]
    return tsv([clean_c0c1("-" if v is None else jq_tostring(v)) for v in cells])
