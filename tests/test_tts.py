"""Tests for the Blade TTS entities."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components import tts
from homeassistant.const import CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import (
    BladeApiError,
    BladeConnectionError,
    SpeechStream,
)
from custom_components.blade.const import (
    CONF_STREAMING,
    CONF_VOICE,
    RECOMMENDED_TTS_OPTIONS,
    SPEECH_VOICES,
)
from custom_components.blade.tts import (
    BladeStreamingTTSEntity,
    BladeTTSEntity,
    split_sentences,
    wav_header,
)

from .conftest import subentry_id

ENTITY_ID = "tts.blade_tts"
LONG_A = "This first sentence is comfortably longer than forty characters."
LONG_B = "And the second one is also long enough to stand on its own."


async def configure(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, Any],
) -> BladeTTSEntity:
    """Replace the TTS subentry, set the entry up and return the entity."""
    hass.config_entries.async_update_subentry(
        entry, entry.subentries[subentry_id(entry, "tts")], data=data
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)


async def drain(stream: AsyncGenerator[bytes], into: list[bytes]) -> None:
    """Collect a byte stream, keeping what arrived before a failure."""
    async for chunk in stream:
        into.append(chunk)  # noqa: PERF401


async def chunks_of(*parts: str) -> AsyncGenerator[str]:
    """Stream text chunks."""
    for part in parts:
        yield part


@pytest.mark.parametrize(
    ("data", "streaming"),
    [
        (dict(RECOMMENDED_TTS_OPTIONS), True),
        ({**RECOMMENDED_TTS_OPTIONS, CONF_STREAMING: False}, False),
        # Streaming is only available on models served by the WebSocket.
        (
            {CONF_MODEL: "kokoro-82m", CONF_VOICE: "af_heart", CONF_STREAMING: True},
            False,
        ),
        ({CONF_MODEL: "kokoro-82m", CONF_VOICE: "af_heart"}, False),
    ],
)
async def test_entity_class(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, Any],
    streaming: bool,
) -> None:
    """The streaming entity is used only when asked and supported."""
    entity = await configure(hass, mock_config_entry, mock_client, data)
    assert isinstance(entity, BladeStreamingTTSEntity) is streaming
    assert isinstance(entity, BladeTTSEntity)


@pytest.mark.parametrize(
    ("data", "languages", "voices", "default"),
    [
        (
            dict(RECOMMENDED_TTS_OPTIONS),
            ["fr-FR", "en-US", "en-GB"],
            [
                "unmute-prod-website/developpeuse-3.wav",
                "unmute-prod-website/developer-1.mp3",
            ],
            {tts.ATTR_VOICE: "unmute-prod-website/developpeuse-3.wav"},
        ),
        (
            {CONF_MODEL: "kokoro-82m", CONF_VOICE: "bf_emma"},
            ["en-US", "en-GB"],
            list(SPEECH_VOICES["kokoro-82m"]),
            {tts.ATTR_VOICE: "bf_emma"},
        ),
        # No voice configured: the first documented voice is the default.
        (
            {CONF_MODEL: "kokoro-82m"},
            ["en-US", "en-GB"],
            None,
            {tts.ATTR_VOICE: "af_heart"},
        ),
        # A voice typed by hand is offered first.
        (
            {
                CONF_MODEL: "kokoro-82m",
                CONF_VOICE: "custom_voice",
                CONF_STREAMING: False,
            },
            ["en-US", "en-GB"],
            ["custom_voice", "af_heart"],  # the typed voice comes first
            {tts.ATTR_VOICE: "custom_voice"},
        ),
        # A model with no documented voices.
        (
            {CONF_MODEL: "text-only", CONF_VOICE: "mine"},
            ["en-US"],
            ["mine"],
            {tts.ATTR_VOICE: "mine"},
        ),
        ({CONF_MODEL: "text-only"}, ["en-US"], [], {}),
    ],
)
async def test_voices_and_languages(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, Any],
    languages: list[str],
    voices: list[str] | None,
    default: dict[str, str],
) -> None:
    """Languages, voices and default options follow the configured model."""
    entity = await configure(hass, mock_config_entry, mock_client, data)
    assert entity.supported_languages == languages
    assert entity.default_language == languages[0]
    assert entity.supported_options == [tts.ATTR_VOICE]
    assert entity.default_options == default
    listed = [v.voice_id for v in entity.async_get_supported_voices(languages[0])]
    if voices is not None:
        assert listed[: len(voices)] == voices
        assert len(listed) == len(voices) or voices[0] == "custom_voice"
    assert entity.name == "Blade TTS"


async def test_synthesize_chosen_voice(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    """A complete WAV comes back, produced with the voice asked for."""
    entity = await configure(
        hass,
        mock_config_entry,
        mock_client,
        {CONF_MODEL: "kokoro-82m", CONF_VOICE: "af_heart"},
    )
    result = await entity.async_get_tts_audio(
        "Hello", "en-US", {tts.ATTR_VOICE: "am_adam"}
    )
    assert result == ("wav", b"RIFFwav")
    mock_client.speech.assert_awaited_once_with("Hello", "kokoro-82m", "am_adam")


async def test_synthesize_default_voice(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Without an explicit voice the configured one is used."""
    entity = hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)
    result = await entity.async_get_tts_audio("Hi", "fr-FR", entity.default_options)
    assert result == ("wav", b"RIFFwav")
    assert mock_client.speech.await_args.args == (
        "Hi",
        "kyutai-tts-1.6b-en-fr",
        "unmute-prod-website/developpeuse-3.wav",
    )


async def test_synthesize_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An API failure is raised as a Home Assistant error."""
    mock_client.speech.side_effect = BladeApiError(500, "boom", "no voice")
    entity = hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)
    with pytest.raises(HomeAssistantError, match="no voice"):
        await entity.async_get_tts_audio("Hi", "fr-FR", {tts.ATTR_VOICE: "v"})


# ----------------------------------------------------------------- Streaming


def install_stream(
    mock_client: MagicMock, *audio: bytes, error: Exception | None = None
) -> list[list[str]]:
    """Make speech_stream consume the utterances and play back audio chunks."""
    utterances: list[list[str]] = []

    async def speech_stream(texts: Any, model: str, voice: str) -> SpeechStream:
        utterances.append([text async for text in texts])
        utterances.append([model, voice])

        async def play() -> AsyncGenerator[bytes]:
            for chunk in audio:
                yield chunk
            if error:
                raise error

        return SpeechStream(sample_rate=24000, channels=1, chunks=play())

    mock_client.speech_stream.side_effect = speech_stream
    return utterances


async def test_stream_tts(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The stream starts with a WAV header, then raw PCM, sentences regrouped."""
    log = install_stream(mock_client, b"\x01\x00", b"\x02\x00")
    entity = hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)
    assert isinstance(entity, BladeStreamingTTSEntity)
    response = await entity.async_stream_tts_audio(
        tts.TTSAudioRequest(
            language="fr-FR",
            options={tts.ATTR_VOICE: "unmute-prod-website/developer-1.mp3"},
            message_gen=chunks_of(f"{LONG_A} ", f"{LONG_B} Short."),
        )
    )
    assert response.extension == "wav"
    data = [chunk async for chunk in response.data_gen]
    assert data[1:] == [b"\x01\x00", b"\x02\x00"]
    with wave.open(io.BytesIO(data[0])) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            24000,
            1,
            2,
        )
        assert wav.getnframes() == 0
    assert log[0] == [LONG_A, LONG_B, "Short."]
    assert log[1] == ["kyutai-tts-1.6b-en-fr", "unmute-prod-website/developer-1.mp3"]


async def test_stream_tts_default_voice(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A request without options uses the configured voice."""
    log = install_stream(mock_client)
    entity = hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)
    response = await entity.async_stream_tts_audio(
        tts.TTSAudioRequest("fr-FR", {}, chunks_of("Bonjour."))
    )
    _ = [chunk async for chunk in response.data_gen]
    assert log[1][1] == "unmute-prod-website/developpeuse-3.wav"


async def test_stream_tts_open_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A WebSocket that cannot open is a Home Assistant error."""
    mock_client.speech_stream.side_effect = BladeConnectionError("refused")
    entity = hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)
    with pytest.raises(HomeAssistantError, match="refused"):
        await entity.async_stream_tts_audio(
            tts.TTSAudioRequest("fr-FR", {}, chunks_of("Bonjour."))
        )


async def test_stream_tts_midstream_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A failure while audio flows is raised from the byte stream."""
    install_stream(
        mock_client, b"\x01\x00", error=BladeApiError(200, "speech_stream", "cut")
    )
    entity = hass.data["entity_components"]["tts"].get_entity(ENTITY_ID)
    response = await entity.async_stream_tts_audio(
        tts.TTSAudioRequest("fr-FR", {}, chunks_of("Bonjour."))
    )
    received: list[bytes] = []
    with pytest.raises(HomeAssistantError, match="cut"):
        await drain(response.data_gen, received)
    assert len(received) == 2


# ------------------------------------------------------------------- Helpers


def test_wav_header() -> None:
    """The header describes 16-bit PCM and no frame."""
    with wave.open(io.BytesIO(wav_header(16000, 2))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            16000,
            2,
            2,
        )
        assert wav.getnframes() == 0


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        # Long sentences leave as soon as they are complete.
        ([f"{LONG_A} {LONG_B} tail"], [LONG_A, LONG_B, "tail"]),
        # Short sentences are merged with their neighbours.
        (
            ["Hi. Ok. Sure.", " Fine. Done. And now a longer closing sentence!"],
            ["Hi. Ok. Sure. Fine. Done. And now a longer closing sentence!"],
        ),
        # Newlines end a sentence too.
        ([f"{LONG_A}\n{LONG_B}\n"], [LONG_A, LONG_B]),
        # Nothing but whitespace is left over: nothing is emitted.
        ([f"{LONG_A}  "], [LONG_A]),
        (["   "], []),
        ([], []),
        # Chunks cut anywhere reassemble into the same sentences.
        (list(f"{LONG_A} {LONG_B} "), [LONG_A, LONG_B]),
    ],
)
async def test_split_sentences(chunks: list[str], expected: list[str]) -> None:
    """Sentences are regrouped from arbitrary text chunks."""
    assert [text async for text in split_sentences(chunks_of(*chunks))] == expected


async def test_split_sentences_keeps_space_across_chunks() -> None:
    """Whitespace ending a chunk must still separate the next chunk's text."""
    out = [t async for t in split_sentences(chunks_of("Hi. ", "Ok then."))]
    assert out == ["Hi. Ok then."]
