"""Tests for the Blade STT entity."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from homeassistant.components import stt
from homeassistant.const import CONF_MODEL, CONF_PROMPT
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import BladeApiError
from custom_components.blade.stt import LANGUAGES

from .conftest import subentry_id

ENTITY_ID = "stt.blade_stt"


def metadata(
    fmt: stt.AudioFormats = stt.AudioFormats.WAV,
    codec: stt.AudioCodecs = stt.AudioCodecs.PCM,
    language: str = "fr-FR",
) -> stt.SpeechMetadata:
    """Build the metadata Assist would send."""
    return stt.SpeechMetadata(
        language=language,
        format=fmt,
        codec=codec,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )


async def audio(*chunks: bytes) -> AsyncIterator[bytes]:
    """Stream audio chunks."""
    for chunk in chunks:
        yield chunk


def entity(hass: HomeAssistant) -> stt.SpeechToTextEntity:
    """Return the live STT entity."""
    return hass.data["entity_components"]["stt"].get_entity(ENTITY_ID)


async def test_capabilities(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The entity advertises what the transcription route accepts."""
    stt_entity = entity(hass)
    languages = stt_entity.supported_languages
    assert set(LANGUAGES) <= set(languages)
    # A pipeline saved with a bare language must be accepted too.
    assert {"fr", "en", "pt"} <= set(languages)
    assert len(languages) == len(set(languages))
    assert stt_entity.supported_formats == [stt.AudioFormats.WAV, stt.AudioFormats.OGG]
    assert stt_entity.supported_codecs == [stt.AudioCodecs.PCM, stt.AudioCodecs.OPUS]
    assert stt_entity.supported_bit_rates == [stt.AudioBitRates.BITRATE_16]
    assert stt_entity.supported_channels == [
        stt.AudioChannels.CHANNEL_MONO,
        stt.AudioChannels.CHANNEL_STEREO,
    ]
    assert stt.AudioSampleRates.SAMPLERATE_48000 in stt_entity.supported_sample_rates


async def test_transcribe_wav(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Headerless PCM is wrapped in a WAV container before upload."""
    pcm = b"\x01\x00" * 100
    result = await entity(hass).async_process_audio_stream(
        metadata(), audio(pcm[:50], pcm[50:])
    )
    assert result == stt.SpeechResult(
        "turn on the light", stt.SpeechResultState.SUCCESS
    )

    sent, filename, model = mock_client.transcribe.await_args.args
    assert (filename, model) == ("audio.wav", "voxtral-mini-3b")
    assert mock_client.transcribe.await_args.kwargs == {
        "language": "fr",
        "prompt": None,
    }
    with wave.open(io.BytesIO(sent)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == pcm


async def test_transcribe_ogg_is_sent_untouched(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """OGG audio already is a container and is forwarded as is."""
    result = await entity(hass).async_process_audio_stream(
        metadata(stt.AudioFormats.OGG, stt.AudioCodecs.OPUS, "en-US"),
        audio(b"OggS", b"data"),
    )
    assert result.result is stt.SpeechResultState.SUCCESS
    assert mock_client.transcribe.await_args.args[:2] == (b"OggSdata", "audio.ogg")
    assert mock_client.transcribe.await_args.kwargs["language"] == "en"


async def test_prompt_is_forwarded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The configured prompt biases the transcription."""
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        mock_config_entry.subentries[subentry_id(mock_config_entry, "stt")],
        data={CONF_MODEL: "whisper-large", CONF_PROMPT: "Smart home vocabulary"},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await entity(hass).async_process_audio_stream(metadata(), audio(b"\x00\x00"))
    assert mock_client.transcribe.await_args.kwargs["prompt"] == "Smart home vocabulary"
    assert mock_client.transcribe.await_args.args[2] == "whisper-large"


@pytest.mark.parametrize("text", ["", None])
async def test_empty_transcript(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    text: str | None,
) -> None:
    """An empty transcript is an error for Assist."""
    mock_client.transcribe.return_value = text
    result = await entity(hass).async_process_audio_stream(
        metadata(), audio(b"\x00\x00")
    )
    assert result == stt.SpeechResult(None, stt.SpeechResultState.ERROR)


async def test_transcription_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An API failure is logged and reported as an error result."""
    mock_client.transcribe.side_effect = BladeApiError(500, "boom", "kaput")
    result = await entity(hass).async_process_audio_stream(
        metadata(), audio(b"\x00\x00")
    )
    assert result == stt.SpeechResult(None, stt.SpeechResultState.ERROR)
    assert "Blade transcription failed" in caplog.text
