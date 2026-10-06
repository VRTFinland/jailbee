"""Temporary backport of BerriAI/litellm#41235 onto litellm 1.104.0.

Runs inside the proxy container (``/opt/litellm/bin/python``) right after the
package install. The ChatGPT backend forces SSE, so a caller that did not ask
for a stream still got a streaming iterator back, and the Responses bridge
raised "Unknown items in responses API response: []" on the empty
``response.completed`` event. The fix honours the caller's stream flag: when it
did not ask for a stream, the handler drains the response instead.

Only ``llm_http_handler.py`` is touched. The upstream change is not on any
release up to 1.104.0; delete this file, its call in ``install.sh``, its
heredoc in ``litellm._provision`` and its tests once the pin reaches a litellm
that carries it (``tests/test_litellm_chatgpt_stream_fix.py`` fails on the next
pin bump to remind whoever makes it).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path

TARGET_VERSION = "1.104.0"
HANDLER = Path("llms") / "custom_httpx" / "llm_http_handler.py"
MARKER = "caller_requested_stream"

_STREAM_ANCHOR = '        stream = response_api_optional_request_params.get("stream", False)\n'
_FLAG = (
    '        caller_requested_stream: Final = bool(stream or (extra_body or {}).get("stream"))\n'
)

_KWARGS = (
    "response=response,",
    "model=model,",
    "logging_obj=logging_obj,",
    "responses_api_provider_config=responses_api_provider_config,",
    "litellm_metadata=litellm_metadata,",
    "custom_llm_provider=custom_llm_provider,",
    "request_data=request_context,",
    "call_type=CallTypes.responses.value,",
)

# The sync and async handlers each end their streaming branch the same way:
# a fake-stream return, then the real iterator, then `else:`.
_STREAM_RETURN = re.compile(
    r"(?P<ind> +)if fake_stream is True:\n"
    r" +return MockResponsesAPIStreamingIterator\(\n(?:.*\n)*?"
    r" +\)\n\n(?: +# Return the streaming iterator\n)?"
    r" +return (?P<it>(?:Sync)?ResponsesAPIStreamingIterator)\(\n(?:.*\n)*?"
    r" +\)\n(?= +else:\n)"
)


class PatchError(Exception):
    """The installed litellm does not have the layout this patch was written for."""


def _call(indent: str, head: str) -> str:
    """`head(` plus the shared keyword arguments plus `)`, starting at `indent`."""
    args = "".join(f"{indent}    {line}\n" for line in _KWARGS)
    return f"{indent}{head}(\n{args}{indent})\n"


def _rewrite(match: re.Match[str]) -> str:
    ind, iterator = match["ind"], match["it"]
    drain = (
        "await response.aread()"
        if iterator == "ResponsesAPIStreamingIterator"
        else "response.read()"
    )
    return (
        f"{ind}if caller_requested_stream:\n"
        f"{ind}    if fake_stream is True:\n"
        + _call(ind + "        ", "return MockResponsesAPIStreamingIterator")
        + "\n"
        + _call(ind + "    ", f"return {iterator}")
        + f"{ind}{drain}\n"
    )


def patch_source(source: str) -> str:
    """Return `source` with the fix applied; raise PatchError if it does not fit."""
    anchors = source.count(_STREAM_ANCHOR)
    if anchors != 2:
        raise PatchError(f"expected 2 stream flags in the handlers, found {anchors}")
    source = source.replace(_STREAM_ANCHOR, _STREAM_ANCHOR + _FLAG)
    source, returns = _STREAM_RETURN.subn(_rewrite, source)
    if returns != 2:
        raise PatchError(f"expected 2 streaming returns in the handlers, found {returns}")
    compile(source, str(HANDLER), "exec")
    return source


def main() -> int:
    installed = importlib.metadata.version("litellm")
    if installed != TARGET_VERSION:
        print(f"litellm {installed}: the ChatGPT stream fix is for {TARGET_VERSION}, skipped")
        return 0
    spec = importlib.util.find_spec("litellm")
    if spec is None or spec.origin is None:
        raise PatchError("litellm is installed but not importable")
    handler = Path(spec.origin).parent / HANDLER
    source = handler.read_text()
    if MARKER in source:
        print("litellm ChatGPT stream fix already applied")
        return 0
    handler.write_text(patch_source(source))
    print(f"litellm {installed}: applied the ChatGPT stream fix to {handler}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatchError as error:
        sys.exit(f"litellm ChatGPT stream fix: {error}")
