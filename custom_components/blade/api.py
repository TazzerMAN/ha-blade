"""Async client for the Blade inference API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

import aiohttp

DEFAULT_BASE_URL = "https://api.models.blade.sh/v1"
SPEECH_STREAM_PATH = "/audio/speech/stream"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=15)
# Longest Retry-After honoured before giving up on a 429 or a 503.
MAX_RETRY_DELAY = 10.0
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_PAYMENT_REQUIRED = 402
# 503: no replica yet; 504: a boot (or, rarely, a generation) outlasted the gateway.
COLD_START_STATUSES = frozenset({503, 504})


class BladeError(Exception):
    """Base error raised by the Blade client."""


class BladeConnectionError(BladeError):
    """The API could not be reached."""


class BladeAuthenticationError(BladeError):
    """The API key is missing, invalid or revoked."""


class BladeApiError(BladeError):
    """The API answered with an error envelope."""

    def __init__(self, status: int, code: str | None, message: str) -> None:
        """Store the HTTP status and the Blade error code."""
        super().__init__(f"{status} {code or 'error'}: {message}")
        self.status = status
        self.code = code

    @property
    def cold_start(self) -> bool:
        """Return whether the model was most likely still starting."""
        return self.status in COLD_START_STATUSES


@dataclass(slots=True, frozen=True)
class BladeModel:
    """One entry of the model catalog."""

    id: str
    kind: str
    input_modalities: tuple[str, ...] = ()
    supported_parameters: frozenset[str] = frozenset()
    context_length: int | None = None
    publisher: str | None = None
    reasoning: bool = False
    batch: Mapping[str, Any] = field(default_factory=dict)
    spot_multiplier: float = 1.0

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Self:
        """Build a model from a /models item."""
        architecture = data.get("architecture") or {}
        return cls(
            id=data["id"],
            kind=data.get("kind") or "chat",
            input_modalities=tuple(architecture.get("input_modalities") or ()),
            supported_parameters=frozenset(data.get("supported_parameters") or ()),
            context_length=data.get("context_length"),
            publisher=data.get("publisher"),
            reasoning=bool((data.get("reasoning") or {}).get("controllable")),
            batch=dict(data.get("batch") or {}),
            spot_multiplier=float(data.get("spot_multiplier") or 1.0),
        )

    @property
    def supports_tools(self) -> bool:
        """Return whether the model accepts function tools."""
        return "tools" in self.supported_parameters

    @property
    def supports_vision(self) -> bool:
        """Return whether the model accepts images."""
        return "image" in self.input_modalities


@dataclass(slots=True)
class SpeechStream:
    """Audio produced by the streaming speech endpoint."""

    sample_rate: int
    channels: int
    chunks: AsyncGenerator[bytes]


@dataclass(slots=True)
class StreamDelta:
    """One parsed chat completion chunk."""

    role: str | None = None
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None


class BladeBudgetError(BladeError):
    """The monthly spending cap set in Home Assistant is reached."""


class UsageHooks(Protocol):
    """Receives what each call costs and the account problems Blade reports."""

    def before_call(self) -> None:
        """Raise to refuse a call (spending cap)."""

    def usage(self, model: str, **metrics: float) -> None:
        """Record one call's usage."""

    def account_error(self, code: str | None) -> None:
        """Record a payment refusal (HTTP 402)."""


# Label of the feature making the current call, for the per-service breakdown.
current_service: ContextVar[str] = ContextVar("blade_service", default="actions")


class BladeClient:
    """Thin async wrapper over the Blade REST and WebSocket endpoints."""

    hooks: UsageHooks | None = None

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        """Bind the client to a shared aiohttp session."""
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _report(self, model: str, **metrics: float) -> None:
        if self.hooks is not None:
            self.hooks.usage(model, **metrics)

    def _report_tokens(self, model: str, usage: Mapping[str, Any] | None) -> None:
        if usage:
            details = usage.get("prompt_tokens_details") or {}
            self._report(
                model,
                tokens_in=float(
                    usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                ),
                tokens_out=float(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                ),
                tokens_cached=float(details.get("cached_tokens") or 0),
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
    ) -> aiohttp.ClientResponse:
        """Send a request, retrying once on a short Retry-After."""
        if self.hooks is not None:
            self.hooks.before_call()
        for attempt in range(2):
            try:
                response = await self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    json=json_body,
                    data=data,
                    timeout=REQUEST_TIMEOUT,
                )
            except (aiohttp.ClientError, TimeoutError) as err:
                raise BladeConnectionError(str(err)) from err
            if response.status < HTTP_BAD_REQUEST:
                return response
            delay = _retry_delay(response)
            if attempt == 0 and delay is not None:
                response.release()
                await asyncio.sleep(delay)
                continue
            try:
                await _raise_for_error(response)
            except BladeApiError as err:
                if err.status == HTTP_PAYMENT_REQUIRED and self.hooks is not None:
                    self.hooks.account_error(err.code)
                raise
        raise BladeConnectionError("unreachable")  # pragma: no cover

    async def _json(
        self, method: str, path: str, json_body: Any = None
    ) -> dict[str, Any]:
        response = await self._request(method, path, json_body=json_body)
        async with response:
            result: dict[str, Any] = await response.json()
            return result

    async def list_models(self) -> dict[str, BladeModel]:
        """Return the catalog visible to this key, by model id."""
        payload = await self._json("GET", "/models")
        return {
            model.id: model
            for model in (BladeModel.from_api(item) for item in payload["data"])
        }

    async def chat(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Run a non-streaming chat completion."""
        payload = await self._json(
            "POST", "/chat/completions", {**body, "stream": False}
        )
        self._report_tokens(str(body.get("model")), payload.get("usage"))
        return payload

    async def chat_stream(self, body: Mapping[str, Any]) -> AsyncGenerator[StreamDelta]:
        """Run a chat completion and yield its parsed deltas."""
        model = str(body.get("model"))
        response = await self._request(
            "POST",
            "/chat/completions",
            json_body={
                **body,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        async with response:
            async for event in _iter_sse(response.content):
                if (error := event.get("error")) is not None:
                    raise BladeApiError(
                        HTTP_OK, error.get("code"), error.get("message", "")
                    )
                self._report_tokens(model, event.get("usage"))
                search = event.get("web_search") or {}
                if search.get("status") == "done":
                    self._report(
                        model,
                        extra_cost=float(search.get("searches") or 0)
                        * float(search.get("price_per_search") or 0),
                    )
                for choice in event.get("choices") or ():
                    delta = choice.get("delta") or {}
                    yield StreamDelta(
                        role=delta.get("role"),
                        content=delta.get("content") or None,
                        reasoning=delta.get("reasoning")
                        or delta.get("reasoning_content")
                        or None,
                        tool_calls=delta.get("tool_calls") or [],
                        finish_reason=choice.get("finish_reason"),
                    )

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        model: str,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Transcribe a complete audio file."""
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=filename)
        form.add_field("model", model)
        form.add_field("response_format", "json")
        if language:
            form.add_field("language", language)
        if prompt:
            form.add_field("prompt", prompt)
        response = await self._request("POST", "/audio/transcriptions", data=form)
        async with response:
            payload = await response.json()
        usage = payload.get("usage") or {}
        if seconds := usage.get("seconds"):
            self._report(model, audio_seconds=float(seconds))
        return str(payload.get("text") or "").strip()

    async def speech(self, text: str, model: str, voice: str) -> bytes:
        """Synthesize a complete WAV file."""
        response = await self._request(
            "POST",
            "/audio/speech",
            json_body={"model": model, "input": text, "voice": voice},
        )
        async with response:
            audio = await response.read()
        # 24 kHz mono 16-bit PCM after a 44-byte WAV header.
        self._report(model, audio_seconds=max(len(audio) - 44, 0) / 48000)
        return audio

    async def speech_stream(
        self, texts: AsyncIterable[str], model: str, voice: str
    ) -> SpeechStream:
        """Open the WebSocket speech stream and feed it utterances as they come.

        Raw PCM s16le chunks are yielded as soon as the gateway forwards them.
        """
        ws = await self._ws_connect(SPEECH_STREAM_PATH)
        try:
            await ws.send_json({"type": "start", "model": model, "voice": voice})
            ready = await ws.receive_json(timeout=30)
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as err:
            await ws.close()
            raise BladeConnectionError(f"speech stream did not start: {err}") from err
        if ready.get("type") != "ready":
            await ws.close()
            raise BladeApiError(HTTP_OK, "speech_stream", str(ready.get("message")))
        return SpeechStream(
            sample_rate=int(ready.get("sample_rate_hz", 24000)),
            channels=int(ready.get("channels", 1)),
            chunks=_relay_speech(
                ws, texts, lambda s: self._report(model, audio_seconds=s)
            ),
        )

    async def _ws_connect(self, path: str) -> aiohttp.ClientWebSocketResponse:
        url = self._base_url.replace("https://", "wss://", 1) + path
        try:
            return await self._session.ws_connect(
                url,
                headers=self._headers,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status == HTTP_UNAUTHORIZED:
                raise BladeAuthenticationError(str(err)) from err
            raise BladeApiError(err.status, None, str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BladeConnectionError(str(err)) from err

    async def generate_image(self, prompt: str, model: str, size: str) -> bytes:
        """Generate one image and return its raw bytes."""
        payload = await self._json(
            "POST",
            "/images/generations",
            {"model": model, "prompt": prompt, "n": 1, "size": size},
        )
        try:
            encoded: str = payload["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as err:
            raise BladeApiError(HTTP_OK, "no_image", "no image returned") from err
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as err:
            raise BladeApiError(HTTP_OK, "invalid_image", str(err)) from err
        self._report(model, images=1)
        return image

    async def decide(
        self, model: str, state: Any, questions: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Ask typed questions about a record (noul, choice, score)."""
        payload = await self._json(
            "POST",
            "/decisions",
            {"model": model, "state": state, "questions": dict(questions)},
        )
        self._report_tokens(model, payload.get("usage"))
        answers: dict[str, Any] = payload["answers"]
        return answers

    async def forecast(
        self,
        model: str,
        context: list[list[float | None]],
        prediction_length: int,
        quantile_levels: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Forecast each series of the context."""
        body: dict[str, Any] = {
            "model": model,
            "context": context,
            "prediction_length": prediction_length,
        }
        if quantile_levels:
            body["quantile_levels"] = quantile_levels
        payload = await self._json("POST", "/forecast", body)
        data: list[dict[str, Any]] = payload["data"]
        return data


def _retry_delay(response: aiohttp.ClientResponse) -> float | None:
    """Return how long to wait before retrying, if a retry makes sense."""
    if response.status not in (429, 503):
        return None
    try:
        delay = float(response.headers.get("Retry-After", "1"))
    except ValueError:
        delay = 1.0
    return delay if 0 <= delay <= MAX_RETRY_DELAY else None


async def _relay_speech(
    ws: aiohttp.ClientWebSocketResponse,
    texts: AsyncIterable[str],
    on_done: Callable[[float], None],
) -> AsyncGenerator[bytes]:
    """Send utterances in the background and yield the audio frames."""

    async def feed() -> None:
        async for text in texts:
            if text.strip():
                await ws.send_json({"type": "text", "text": text})
        await ws.send_json({"type": "end"})

    sender = asyncio.create_task(feed())
    try:
        async for message in ws:
            if message.type is aiohttp.WSMsgType.BINARY:
                yield message.data
                continue
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            if event.get("type") == "error":
                raise BladeApiError(HTTP_OK, "speech_stream", str(event.get("message")))
            if event.get("type") == "done":
                on_done(float(event.get("audio_seconds") or 0))
                await sender
                return
        raise BladeConnectionError("speech stream closed before done")
    finally:
        sender.cancel()
        await ws.close()


async def _raise_for_error(response: aiohttp.ClientResponse) -> None:
    """Turn an error envelope into the matching exception."""
    async with response:
        try:
            payload = await response.json(content_type=None)
            error = payload.get("error") or {}
            code, message = error.get("code"), error.get("message", "")
        except aiohttp.ContentTypeError, ValueError, AttributeError:
            code, message = None, await response.text()
    if response.status == HTTP_UNAUTHORIZED:
        raise BladeAuthenticationError(message)
    raise BladeApiError(response.status, code, message)


async def _iter_sse(content: aiohttp.StreamReader) -> AsyncGenerator[dict[str, Any]]:
    """Yield the JSON events of a server-sent event stream."""
    async for raw in content:
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue  # keep-alive comments and blank separators
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            return
        yield json.loads(data)
