"""The temporary litellm 1.104.0 backport of BerriAI/litellm#41235."""

from __future__ import annotations

from importlib import resources

import pytest

from jailbee.config.models_litellm import PINNED_LITELLM_VERSION
from jailbee.provision.litellm import chatgpt_stream_fix as fix

_HANDLER = """\
class Handler:
    def response_api_handler(self, extra_body=None):
        # Check if streaming is requested
        stream = response_api_optional_request_params.get("stream", False)

        if stream:
            if True:
                response = sync_httpx_client.post(stream=stream, **body_kwargs)
                if fake_stream is True:
                    return MockResponsesAPIStreamingIterator(
                        response=response,
                        model=model,
                        logging_obj=logging_obj,
                        responses_api_provider_config=responses_api_provider_config,
                        litellm_metadata=litellm_metadata,
                        custom_llm_provider=custom_llm_provider,
                        request_data=request_context,
                        call_type=CallTypes.responses.value,
                    )

                return SyncResponsesAPIStreamingIterator(
                    response=response,
                    model=model,
                    logging_obj=logging_obj,
                    responses_api_provider_config=responses_api_provider_config,
                    litellm_metadata=litellm_metadata,
                    custom_llm_provider=custom_llm_provider,
                    request_data=request_context,
                    call_type=CallTypes.responses.value,
                )
            else:
                response = sync_httpx_client.post()

    async def async_response_api_handler(self, extra_body=None):
        # Check if streaming is requested
        stream = response_api_optional_request_params.get("stream", False)

        if stream:
            if True:
                response = await async_httpx_client.post(stream=stream, **body_kwargs)

                if fake_stream is True:
                    return MockResponsesAPIStreamingIterator(
                        response=response,
                        model=model,
                        logging_obj=logging_obj,
                        responses_api_provider_config=responses_api_provider_config,
                        litellm_metadata=litellm_metadata,
                        custom_llm_provider=custom_llm_provider,
                        request_data=request_context,
                        call_type=CallTypes.responses.value,
                    )

                # Return the streaming iterator
                return ResponsesAPIStreamingIterator(
                    response=response,
                    model=model,
                    logging_obj=logging_obj,
                    responses_api_provider_config=responses_api_provider_config,
                    litellm_metadata=litellm_metadata,
                    custom_llm_provider=custom_llm_provider,
                    request_data=request_context,
                    call_type=CallTypes.responses.value,
                )
            else:
                response = await async_httpx_client.post()
"""


def test_patch_gates_both_streaming_returns_on_the_caller_flag():
    patched = fix.patch_source(_HANDLER)
    assert patched.count("caller_requested_stream: Final = bool(") == 2
    assert patched.count("if caller_requested_stream:") == 2
    assert patched.count("response.read()") == 1
    assert patched.count("await response.aread()") == 1
    # Each iterator is still built once per handler, now only for a streaming caller.
    assert patched.count("return MockResponsesAPIStreamingIterator(") == 2
    assert patched.count("return SyncResponsesAPIStreamingIterator(") == 1
    assert patched.count("return ResponsesAPIStreamingIterator(") == 1


def test_patch_leaves_the_streaming_iterator_unreachable_for_a_non_streaming_caller():
    patched = fix.patch_source(_HANDLER)
    for drain, iterator in (
        ("response.read()", "return SyncResponsesAPIStreamingIterator("),
        ("await response.aread()", "return ResponsesAPIStreamingIterator("),
    ):
        assert patched.index(iterator) < patched.index(drain)
        # The drain sits at the guard's own level, so it runs when the guard is false.
        guard = patched.rindex("if caller_requested_stream:", 0, patched.index(iterator))
        indent = len(patched[patched.rfind("\n", 0, guard) + 1 : guard])
        line = next(line for line in patched.splitlines() if line.strip() == drain)
        assert len(line) - len(line.lstrip()) == indent


def test_patched_source_is_valid_python():
    compile(fix.patch_source(_HANDLER), "handler.py", "exec")


@pytest.mark.parametrize(
    "source",
    [
        "",
        _HANDLER.replace(
            'stream = response_api_optional_request_params.get("stream", False)', "stream = 1"
        ),
    ],
)
def test_patch_refuses_a_layout_it_was_not_written_for(source):
    with pytest.raises(fix.PatchError):
        fix.patch_source(source)


def test_patch_refuses_a_handler_whose_streaming_returns_moved():
    broken = _HANDLER.replace("            else:\n", "            elif other:\n")
    with pytest.raises(fix.PatchError):
        fix.patch_source(broken)


def test_install_runs_the_fix_after_the_package_install():
    script = resources.files("jailbee.provision").joinpath("litellm", "install.sh").read_text()
    assert script.index("pip install") < script.index("/root/litellm-chatgpt-stream-fix.py")


def test_provision_ships_the_fix_into_the_container():
    from unittest.mock import MagicMock

    from jailbee import litellm as ll

    incus = MagicMock()
    ll._provision(incus, "1.104.0", True)
    command = incus.exec_with_input.call_args.args[2]
    assert "cat > /root/litellm-chatgpt-stream-fix.py" in command
    assert "def patch_source" in command


def test_the_fix_is_removed_when_the_pin_moves_on():
    # The backport is for one release. When the pin is bumped, delete
    # chatgpt_stream_fix.py, its call in install.sh, its heredoc in
    # litellm._provision and this file, unless the new pin still lacks #41235.
    assert PINNED_LITELLM_VERSION == fix.TARGET_VERSION, (
        f"the pin is {PINNED_LITELLM_VERSION}, not {fix.TARGET_VERSION}: check that the new "
        "pin carries litellm#41235, then remove the temporary ChatGPT stream backport "
        "(see chatgpt_stream_fix.py)"
    )
