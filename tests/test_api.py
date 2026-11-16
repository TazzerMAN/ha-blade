"""Tests for the Blade HTTP and WebSocket client."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from multidict import CIMultiDict
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.blade import api
from custom_components.blade.api import (
    BladeApiError,
    BladeAuthenticationError,
    BladeClient,
    BladeConnectionError,
    BladeModel,
)

BASE = "https://api.models.blade.sh/v1"


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> BladeClient:
    """Return a real client bound to the mocked aiohttp session."""
    return BladeClient(async_get_clientsession(hass), "key-123")


def sequence(
    *responses: dict[str, Any],
) -> Callable[[str, URL, Any], Any]:
    """Return a side effect answering each call with the next response."""
    pending = list(responses)

    async def side_effect(method: str, url: URL, data: Any) -> Any:
        return AiohttpClientMockResponse(method, url, **pending.pop(0))

    return side_effect


def test_model_from_api() -> None:
    """A catalog item is converted with its optional fields."""
    model = BladeModel.from_api(
        {
            "id": "m",
            "kind": "chat",
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools"],
            "context_length": 8192,
            "publisher": "acme",
            "reasoning": {"controllable": True},
        }
    )
    assert model.supports_tools
    assert model.supports_vision
    assert model.reasoning
    assert model.context_length == 8192
    assert model.publisher == "acme"


def test_model_from_api_defaults() -> None:
    """Missing fields fall back to a plain chat model."""
    model = BladeModel.from_api({"id": "m", "architecture": None, "reasoning": None})
    assert model == BladeModel("m", "chat")
    assert not model.supports_tools
    assert not model.supports_vision


async def test_list_models(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The catalog is keyed by id and the key is sent as a bearer token."""
    aioclient_mock.get(
        f"{BASE}/models", json={"data": [{"id": "a"}, {"id": "b", "kind": "image"}]}
    )
    models = await client.list_models()
    assert {k: v.kind for k, v in models.items()} == {"a": "chat", "b": "image"}
    assert aioclient_mock.mock_calls[0][3] == {"Authorization": "Bearer key-123"}


async def test_trailing_slash_in_base_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A trailing slash on the base URL is ignored."""
    aioclient_mock.get(f"{BASE}/models", json={"data": []})
    custom = BladeClient(async_get_clientsession(hass), "k", f"{BASE}/")
    assert await custom.list_models() == {}


async def test_chat(client: BladeClient, aioclient_mock: AiohttpClientMocker) -> None:
    """A non-streaming chat forces stream=false."""
    aioclient_mock.post(f"{BASE}/chat/completions", json={"choices": []})
    assert await client.chat({"model": "m", "stream": True}) == {"choices": []}
    assert aioclient_mock.mock_calls[0][2] == {"model": "m", "stream": False}


async def test_unauthorized(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 becomes an authentication error carrying the API message."""
    aioclient_mock.get(
        f"{BASE}/models",
        status=401,
        json={"error": {"code": "invalid_key", "message": "bad key"}},
    )
    with pytest.raises(BladeAuthenticationError, match="bad key"):
        await client.list_models()


@pytest.mark.parametrize(
    ("status", "kwargs", "code", "message"),
    [
        (
            400,
            {"json": {"error": {"code": "bad_request", "message": "nope"}}},
            "bad_request",
            "nope",
        ),
        (404, {"json": {"error": {}}}, None, ""),
        (500, {"json": {}}, None, ""),
        (500, {"text": "gateway exploded"}, None, "gateway exploded"),
        (502, {"json": ["not", "a", "dict"]}, None, '["not","a","dict"]'),
    ],
)
async def test_api_errors(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    kwargs: dict[str, Any],
    code: str | None,
    message: str,
) -> None:
    """Other 4xx and 5xx answers become an API error with status and code."""
    aioclient_mock.get(f"{BASE}/models", status=status, **kwargs)
    with pytest.raises(BladeApiError) as err:
        await client.list_models()
    assert err.value.status == status
    assert err.value.code == code
    assert message in str(err.value)
    assert str(err.value).startswith(f"{status} {code or 'error'}")


@pytest.mark.parametrize("exc", [aiohttp.ClientError("boom"), TimeoutError("slow")])
async def test_network_error(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, exc: Exception
) -> None:
    """Transport failures become a connection error."""
    aioclient_mock.get(f"{BASE}/models", exc=exc)
    with pytest.raises(BladeConnectionError):
        await client.list_models()


async def test_retry_after_short(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 429 with a short Retry-After is retried exactly once."""
    aioclient_mock.get(
        f"{BASE}/models",
        side_effect=sequence(
            {"status": 429, "headers": {"Retry-After": "0"}},
            {"json": {"data": [{"id": "a"}]}},
        ),
    )
    assert list(await client.list_models()) == ["a"]
    assert aioclient_mock.call_count == 2


@pytest.mark.parametrize(
    ("header", "delay"),
    [("2", 2.0), ("garbage", 1.0), (None, 1.0)],
)
async def test_retry_waits(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    header: str | None,
    delay: float,
) -> None:
    """The wait follows Retry-After, defaulting to one second."""
    headers = {"Retry-After": header} if header else {}
    aioclient_mock.get(
        f"{BASE}/models",
        side_effect=sequence(
            {"status": 503, "headers": headers}, {"json": {"data": []}}
        ),
    )
    with patch.object(api.asyncio, "sleep", AsyncMock()) as sleep:
        await client.list_models()
    sleep.assert_awaited_once_with(delay)


@pytest.mark.parametrize("header", ["30", "-1"])
async def test_retry_after_out_of_range(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, header: str
) -> None:
    """A Retry-After beyond the cap (or negative) is not honoured."""
    aioclient_mock.get(
        f"{BASE}/models", status=429, headers={"Retry-After": header}, json={}
    )
    with pytest.raises(BladeApiError) as err:
        await client.list_models()
    assert err.value.status == 429
    assert aioclient_mock.call_count == 1


async def test_retry_only_once(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A second 429 is reported instead of retried again."""
    aioclient_mock.get(
        f"{BASE}/models",
        side_effect=sequence(
            {"status": 429, "headers": {"Retry-After": "0"}},
            {"status": 429, "headers": {"Retry-After": "0"}, "json": {}},
        ),
    )
    with pytest.raises(BladeApiError):
        await client.list_models()
    assert aioclient_mock.call_count == 2


async def test_chat_stream(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """SSE events are parsed; comments, blank lines and [DONE] are handled."""
    chunks = [
        ": keep-alive",
        "",
        'data: {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]}',
        "",
        'data: {"choices": [{"delta": {"content": "lo", "reasoning": "think"}}]}',
        'data: {"choices": [{"delta": {"reasoning_content": "more"}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]}',
        'data: {"choices": [{"delta": null, "finish_reason": "stop"}]}',
        'data: {"choices": null}',
        "data: {}",
        "data: [DONE]",
        'data: {"choices": [{"delta": {"content": "never read"}}]}',
    ]
    aioclient_mock.post(f"{BASE}/chat/completions", text="\n".join(chunks) + "\n")
    deltas = [d async for d in client.chat_stream({"model": "m"})]
    assert [d.content for d in deltas] == ["Hel", "lo", None, None, None]
    assert [d.reasoning for d in deltas] == [None, "think", "more", None, None]
    assert deltas[0].role == "assistant"
    assert deltas[2].tool_calls == []
    assert deltas[3].tool_calls == [{"index": 0}]
    assert deltas[4].finish_reason == "stop"
    assert aioclient_mock.mock_calls[0][2] == {
        "model": "m",
        "stream": True,
        "stream_options": {"include_usage": True},
    }


async def test_chat_stream_without_done(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A stream that simply ends is accepted."""
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        text='data: {"choices": [{"delta": {"content": "x"}}]}\n',
    )
    assert [d.content async for d in client.chat_stream({})] == ["x"]


async def test_chat_stream_error_event(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An error event inside the stream raises an API error."""
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        text='data: {"error": {"code": "overloaded", "message": "try later"}}\n',
    )
    with pytest.raises(BladeApiError, match="overloaded: try later") as err:
        _ = [d async for d in client.chat_stream({})]
    assert err.value.status == 200


async def test_chat_stream_http_error(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTTP error before the stream starts is raised on first iteration."""
    aioclient_mock.post(f"{BASE}/chat/completions", status=401, json={})
    with pytest.raises(BladeAuthenticationError):
        _ = [d async for d in client.chat_stream({})]


@pytest.mark.parametrize(("language", "prompt"), [(None, None), ("fr", "context")])
async def test_transcribe(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    language: str | None,
    prompt: str | None,
) -> None:
    """The multipart form carries the optional language and prompt."""
    aioclient_mock.post(f"{BASE}/audio/transcriptions", json={"text": "  salut \n"})
    text = await client.transcribe(b"wav", "a.wav", "whisper", language, prompt)
    assert text == "salut"
    form = aioclient_mock.mock_calls[0][2]
    assert isinstance(form, aiohttp.FormData)
    fields = {field[0]["name"]: field[2] for field in form._fields}
    assert fields["model"] == "whisper"
    assert fields["response_format"] == "json"
    assert fields.get("language") == language
    assert fields.get("prompt") == prompt


async def test_transcribe_empty(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A missing text field gives an empty transcript."""
    aioclient_mock.post(f"{BASE}/audio/transcriptions", json={"text": None})
    assert await client.transcribe(b"", "a.wav", "m") == ""


async def test_speech(client: BladeClient, aioclient_mock: AiohttpClientMocker) -> None:
    """The WAV bytes are returned untouched."""
    aioclient_mock.post(f"{BASE}/audio/speech", content=b"RIFF....")
    assert await client.speech("hi", "kokoro", "af_heart") == b"RIFF...."
    assert aioclient_mock.mock_calls[0][2] == {
        "model": "kokoro",
        "input": "hi",
        "voice": "af_heart",
    }


async def test_generate_image(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The base64 payload is decoded."""
    encoded = base64.b64encode(b"pixels").decode()
    aioclient_mock.post(
        f"{BASE}/images/generations", json={"data": [{"b64_json": encoded}]}
    )
    assert await client.generate_image("a cat", "flux", "512x512") == b"pixels"
    assert aioclient_mock.mock_calls[0][2] == {
        "model": "flux",
        "prompt": "a cat",
        "n": 1,
        "size": "512x512",
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": []}, {"data": [{}]}, {"data": [None]}, {"data": None}],
)
async def test_generate_image_missing(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, payload: dict[str, Any]
) -> None:
    """A response without an image raises a dedicated error."""
    aioclient_mock.post(f"{BASE}/images/generations", json=payload)
    with pytest.raises(BladeApiError, match="no_image"):
        await client.generate_image("a cat", "flux", "512x512")


async def test_generate_image_invalid_base64(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Corrupt base64 surfaces as a Blade error, not a raw binascii error."""
    aioclient_mock.post(
        f"{BASE}/images/generations", json={"data": [{"b64_json": "not base64!"}]}
    )
    with pytest.raises(BladeApiError, match="invalid_image"):
        await client.generate_image("a cat", "flux", "512x512")


async def test_decide(client: BladeClient, aioclient_mock: AiohttpClientMocker) -> None:
    """Questions are sent as a dict and the answers returned."""
    aioclient_mock.post(f"{BASE}/decisions", json={"answers": {"q": {"type": "noul"}}})
    answers = await client.decide("clef", "state", {"q": {"type": "noul"}})
    assert answers == {"q": {"type": "noul"}}
    assert aioclient_mock.mock_calls[0][2]["state"] == "state"


@pytest.mark.parametrize("quantiles", [None, [0.5]])
async def test_forecast(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    quantiles: list[float] | None,
) -> None:
    """Quantile levels are only sent when requested."""
    aioclient_mock.post(f"{BASE}/forecast", json={"data": [{"quantiles": {}}]})
    assert await client.forecast("ts", [[1.0, None]], 3, quantiles) == [
        {"quantiles": {}}
    ]
    body = aioclient_mock.mock_calls[0][2]
    assert body["prediction_length"] == 3
    assert ("quantile_levels" in body) is bool(quantiles)


# ---------------------------------------------------------------- WebSocket


class FakeWebSocket:
    """Minimal stand-in for aiohttp's ClientWebSocketResponse."""

    def __init__(
        self,
        messages: list[aiohttp.WSMessage] | None = None,
        ready: Any = None,
        receive_error: Exception | None = None,
    ) -> None:
        """Script the handshake answer and the messages that follow."""
        self.messages = messages or []
        self.sent: list[dict[str, Any]] = []
        self.send_json = AsyncMock(side_effect=self.sent.append)
        self.receive_json = AsyncMock(return_value=ready, side_effect=receive_error)
        self.close = AsyncMock()

    def __aiter__(self) -> AsyncGenerator[aiohttp.WSMessage]:
        """Iterate over the scripted frames."""

        async def generate() -> AsyncGenerator[aiohttp.WSMessage]:
            for message in self.messages:
                yield message

        return generate()


def binary(data: bytes) -> aiohttp.WSMessage:
    """Return a binary frame."""
    return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, data, None)


def text(payload: dict[str, Any]) -> aiohttp.WSMessage:
    """Return a text frame carrying JSON."""
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(payload), None)


def make_client(ws: FakeWebSocket | Exception) -> tuple[BladeClient, MagicMock]:
    """Return a client whose session opens the fake WebSocket."""
    session = MagicMock()
    if isinstance(ws, Exception):
        session.ws_connect = AsyncMock(side_effect=ws)
    else:
        session.ws_connect = AsyncMock(return_value=ws)
    return BladeClient(session, "key-123"), session


async def drain(stream: AsyncGenerator[bytes], into: list[bytes]) -> None:
    """Collect a byte stream, keeping what arrived before a failure."""
    async for chunk in stream:
        into.append(chunk)  # noqa: PERF401


async def texts(*items: str) -> AsyncGenerator[str]:
    """Yield the given utterances."""
    for item in items:
        yield item


async def test_speech_stream() -> None:
    """Ready, audio, then done: PCM chunks flow and utterances are sent."""
    ws = FakeWebSocket(
        messages=[
            binary(b"\x01\x02"),
            aiohttp.WSMessage(aiohttp.WSMsgType.PING, b"", None),
            text({"type": "progress"}),
            binary(b"\x03"),
            text({"type": "done"}),
        ],
        ready={"type": "ready", "sample_rate_hz": 16000, "channels": 2},
    )
    client, session = make_client(ws)
    stream = await client.speech_stream(texts("Hello.", "  ", "World."), "kyutai", "v")
    assert (stream.sample_rate, stream.channels) == (16000, 2)
    assert [chunk async for chunk in stream.chunks] == [b"\x01\x02", b"\x03"]
    assert ws.sent == [
        {"type": "start", "model": "kyutai", "voice": "v"},
        {"type": "text", "text": "Hello."},
        {"type": "text", "text": "World."},
        {"type": "end"},
    ]
    ws.close.assert_awaited_once()
    assert (
        session.ws_connect.await_args.args[0]
        == "wss://api.models.blade.sh/v1/audio/speech/stream"
    )
    assert session.ws_connect.await_args.kwargs["headers"] == {
        "Authorization": "Bearer key-123"
    }


async def test_speech_stream_default_format() -> None:
    """A ready message without audio parameters means 24 kHz mono."""
    ws = FakeWebSocket(ready={"type": "ready"})
    client, _ = make_client(ws)
    stream = await client.speech_stream(texts(), "m", "v")
    assert (stream.sample_rate, stream.channels) == (24000, 1)
    await stream.chunks.aclose()


async def test_speech_stream_error_event() -> None:
    """An error event ends the stream with an API error."""
    ws = FakeWebSocket(
        messages=[binary(b"a"), text({"type": "error", "message": "no voice"})],
        ready={"type": "ready"},
    )
    client, _ = make_client(ws)
    stream = await client.speech_stream(texts("Hi."), "m", "v")
    received: list[bytes] = []
    with pytest.raises(BladeApiError, match="no voice"):
        await drain(stream.chunks, received)
    assert received == [b"a"]
    ws.close.assert_awaited_once()


async def test_speech_stream_closed_before_done() -> None:
    """A connection that closes without done is an error."""
    ws = FakeWebSocket(messages=[binary(b"a")], ready={"type": "ready"})
    client, _ = make_client(ws)
    stream = await client.speech_stream(texts(), "m", "v")
    with pytest.raises(BladeConnectionError, match="closed before done"):
        _ = [chunk async for chunk in stream.chunks]
    ws.close.assert_awaited_once()


async def test_speech_stream_invalid_ready() -> None:
    """Anything but a ready message is rejected and the socket closed."""
    ws = FakeWebSocket(ready={"type": "error", "message": "bad model"})
    client, _ = make_client(ws)
    with pytest.raises(BladeApiError, match="bad model"):
        await client.speech_stream(texts(), "m", "v")
    ws.close.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [aiohttp.ClientError("x"), TimeoutError(), TypeError("x"), ValueError("x")],
)
async def test_speech_stream_handshake_failure(error: Exception) -> None:
    """A failure waiting for ready closes the socket and is a connection error."""
    ws = FakeWebSocket(receive_error=error)
    client, _ = make_client(ws)
    with pytest.raises(BladeConnectionError, match="did not start"):
        await client.speech_stream(texts(), "m", "v")
    ws.close.assert_awaited_once()


def handshake_error(status: int) -> aiohttp.WSServerHandshakeError:
    """Build a rejected WebSocket upgrade."""
    return aiohttp.WSServerHandshakeError(
        MagicMock(real_url="wss://x"),
        (),
        status=status,
        message="denied",
        headers=CIMultiDict(),
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (handshake_error(401), BladeAuthenticationError),
        (handshake_error(503), BladeApiError),
        (aiohttp.ClientError("down"), BladeConnectionError),
        (TimeoutError(), BladeConnectionError),
    ],
)
async def test_speech_stream_connect_errors(
    error: Exception, expected: type[Exception]
) -> None:
    """Connection failures map to the matching Blade error."""
    client, _ = make_client(error)
    with pytest.raises(expected) as err:
        await client.speech_stream(texts(), "m", "v")
    if expected is BladeApiError:
        assert err.value.status == 503


# -------------------------------------------------------------- Usage reports


@pytest.fixture
def hooks(client: BladeClient) -> MagicMock:
    """Attach a recording usage hook to the client."""
    client.hooks = MagicMock()
    return client.hooks


async def test_no_hooks_is_silent(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without hooks calls work and report nothing."""
    aioclient_mock.post(
        f"{BASE}/chat/completions", json={"usage": {"prompt_tokens": 1}}
    )
    await client.chat({"model": "m"})


async def test_before_call_refuses(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """A refusal from before_call stops the request."""
    hooks.before_call.side_effect = api.BladeBudgetError("cap")
    with pytest.raises(api.BladeBudgetError):
        await client.list_models()
    assert aioclient_mock.call_count == 0


async def test_before_call_once_per_request(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """A retry after Retry-After does not re-check the cap."""
    aioclient_mock.get(
        f"{BASE}/models",
        side_effect=sequence(
            {"status": 429, "headers": {"Retry-After": "0"}, "json": {}},
            {"json": {"data": []}},
        ),
    )
    await client.list_models()
    hooks.before_call.assert_called_once_with()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ({"code": "insufficient_balance", "message": "empty"}, "insufficient_balance"),
        ({"message": "no code"}, None),
    ],
)
async def test_payment_required(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    hooks: MagicMock,
    error: dict[str, str],
    code: str | None,
) -> None:
    """A 402 is raised and reported to the account hook."""
    aioclient_mock.post(f"{BASE}/chat/completions", status=402, json={"error": error})
    with pytest.raises(BladeApiError) as err:
        await client.chat({"model": "m"})
    assert err.value.status == 402
    hooks.account_error.assert_called_once_with(code)


async def test_other_errors_not_reported_as_account(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """Only a 402 reaches account_error."""
    aioclient_mock.get(f"{BASE}/models", status=500, json={})
    with pytest.raises(BladeApiError):
        await client.list_models()
    hooks.account_error.assert_not_called()


async def test_payment_required_without_hooks(
    client: BladeClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 402 without hooks is simply raised."""
    aioclient_mock.get(f"{BASE}/models", status=402, json={})
    with pytest.raises(BladeApiError):
        await client.list_models()


async def test_chat_reports_usage(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """Prompt and completion tokens are reported; input/output names work too."""
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        json={
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 6},
            }
        },
    )
    await client.chat({"model": "m"})
    hooks.usage.assert_called_once_with(
        "m", tokens_in=10.0, tokens_out=4.0, tokens_cached=6.0
    )

    hooks.usage.reset_mock()
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        json={"usage": {"input_tokens": 7, "output_tokens": 3}},
    )
    await client.chat({"model": "m"})
    hooks.usage.assert_called_once_with(
        "m", tokens_in=7.0, tokens_out=3.0, tokens_cached=0.0
    )


@pytest.mark.parametrize("payload", [{}, {"usage": None}, {"usage": {}}])
async def test_chat_without_usage(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    hooks: MagicMock,
    payload: dict[str, Any],
) -> None:
    """A response without usage reports nothing."""
    aioclient_mock.post(f"{BASE}/chat/completions", json=payload)
    await client.chat({"model": "m"})
    hooks.usage.assert_not_called()


async def test_chat_stream_reports_usage_and_searches(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """The final usage and each finished web search are reported."""
    events: list[dict[str, Any]] = [
        {"web_search": {"status": "searching"}},
        {"web_search": {"status": "done", "searches": 3, "price_per_search": 0.01}},
        {"web_search": {"status": "done"}},
        {
            "choices": [{"delta": {"content": "x"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        },
        {"choices": [], "usage": None},
    ]
    chunks = [f"data: {json.dumps(event)}" for event in events] + ["data: [DONE]"]
    aioclient_mock.post(f"{BASE}/chat/completions", text="\n".join(chunks) + "\n")
    deltas = [d async for d in client.chat_stream({"model": "m"})]
    assert [d.content for d in deltas] == ["x"]
    assert hooks.usage.call_args_list[0].args == ("m",)
    assert hooks.usage.call_args_list[0].kwargs == pytest.approx({"extra_cost": 0.03})
    assert hooks.usage.call_args_list[1].kwargs == {"extra_cost": 0.0}
    assert hooks.usage.call_args_list[2].kwargs == {
        "tokens_in": 20.0,
        "tokens_out": 5.0,
        "tokens_cached": 0.0,
    }
    assert hooks.usage.call_count == 3


async def test_transcribe_reports_seconds(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """The audio duration of the usage block is reported when present."""
    aioclient_mock.post(
        f"{BASE}/audio/transcriptions",
        json={"text": "hi", "usage": {"seconds": 12}},
    )
    await client.transcribe(b"", "a.wav", "whisper")
    hooks.usage.assert_called_once_with("whisper", audio_seconds=12.0)

    hooks.usage.reset_mock()
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/audio/transcriptions", json={"text": "hi"})
    await client.transcribe(b"", "a.wav", "whisper")
    hooks.usage.assert_not_called()


@pytest.mark.parametrize(
    ("size", "seconds"), [(44 + 96_000, 2.0), (10, 0.0)], ids=["two-seconds", "tiny"]
)
async def test_speech_reports_duration(
    client: BladeClient,
    aioclient_mock: AiohttpClientMocker,
    hooks: MagicMock,
    size: int,
    seconds: float,
) -> None:
    """The duration is deduced from a 24 kHz mono 16-bit WAV."""
    aioclient_mock.post(f"{BASE}/audio/speech", content=b"x" * size)
    await client.speech("hi", "kokoro-82m", "v")
    hooks.usage.assert_called_once_with("kokoro-82m", audio_seconds=seconds)


async def test_generate_image_reports_image(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """One generated image is reported."""
    aioclient_mock.post(
        f"{BASE}/images/generations",
        json={"data": [{"b64_json": base64.b64encode(b"png").decode()}]},
    )
    await client.generate_image("cat", "flux", "1024x1024")
    hooks.usage.assert_called_once_with("flux", images=1)


async def test_decide_reports_usage(
    client: BladeClient, aioclient_mock: AiohttpClientMocker, hooks: MagicMock
) -> None:
    """A decision reports its tokens."""
    aioclient_mock.post(
        f"{BASE}/decisions",
        json={"answers": {}, "usage": {"prompt_tokens": 9, "completion_tokens": 1}},
    )
    await client.decide("clef-flash", "state", {})
    hooks.usage.assert_called_once_with(
        "clef-flash", tokens_in=9.0, tokens_out=1.0, tokens_cached=0.0
    )


@pytest.mark.parametrize(("seconds", "expected"), [(3.5, 3.5), (None, 0.0)])
async def test_speech_stream_reports_audio_seconds(
    seconds: float | None, expected: float
) -> None:
    """The done event of the WebSocket reports the audio duration."""
    done: dict[str, Any] = {"type": "done"}
    if seconds is not None:
        done["audio_seconds"] = seconds
    ws = FakeWebSocket(messages=[binary(b"a"), text(done)], ready={"type": "ready"})
    client, _ = make_client(ws)
    client.hooks = MagicMock()
    stream = await client.speech_stream(texts("Hi."), "kyutai", "v")
    assert [chunk async for chunk in stream.chunks] == [b"a"]
    client.hooks.usage.assert_called_once_with("kyutai", audio_seconds=expected)


async def test_report_without_hooks_on_speech_stream() -> None:
    """The WebSocket report is a no-op when no hooks are attached."""
    ws = FakeWebSocket(
        messages=[text({"type": "done", "audio_seconds": 1})],
        ready={"type": "ready"},
    )
    client, _ = make_client(ws)
    stream = await client.speech_stream(texts(), "m", "v")
    assert [chunk async for chunk in stream.chunks] == []
