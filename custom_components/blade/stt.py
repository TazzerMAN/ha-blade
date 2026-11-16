"""Speech-to-text with Blade transcription models."""

from __future__ import annotations

import io
import wave
from typing import TYPE_CHECKING, override

from homeassistant.components import stt
from homeassistant.const import CONF_PROMPT

from .api import BladeError, current_service
from .const import LOGGER, SUBENTRY_STT
from .entity import BladeEntity

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import BladeConfigEntry

PARALLEL_UPDATES = 0

# Whisper and Voxtral cover these languages; the list feeds the Assist pipeline UI.
LANGUAGES = [
    "ar-SA", "bg-BG", "ca-ES", "cs-CZ", "cy-GB", "da-DK", "de-DE", "el-GR",
    "en-GB", "en-US", "es-ES", "et-EE", "fa-IR", "fi-FI", "fr-FR", "gl-ES",
    "he-IL", "hi-IN", "hr-HR", "hu-HU", "id-ID", "is-IS", "it-IT", "ja-JP",
    "ko-KR", "lt-LT", "lv-LV", "nl-NL", "no-NO", "pl-PL", "pt-BR", "pt-PT",
    "ro-RO", "ru-RU", "sk-SK", "sl-SI", "sr-RS", "sv-SE", "th-TH", "tr-TR",
    "uk-UA", "vi-VN", "zh-CN",
]  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BladeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one STT entity per STT subentry."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type == SUBENTRY_STT:
            async_add_entities(
                [BladeSTTEntity(config_entry, subentry)],
                config_subentry_id=subentry_id,
            )


class BladeSTTEntity(stt.SpeechToTextEntity, BladeEntity):
    """Transcribe Assist audio with Blade."""

    @property
    @override
    def supported_languages(self) -> list[str]:
        """Return the languages offered in the pipeline settings."""
        # Pipelines may store a bare language ("fr") as well as a region one.
        return LANGUAGES + sorted({lang.split("-")[0] for lang in LANGUAGES})

    @property
    @override
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return the accepted containers."""
        return [stt.AudioFormats.WAV, stt.AudioFormats.OGG]

    @property
    @override
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return the accepted codecs."""
        return [stt.AudioCodecs.PCM, stt.AudioCodecs.OPUS]

    @property
    @override
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return the accepted sample widths."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    @override
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return the accepted sample rates."""
        return [
            stt.AudioSampleRates.SAMPLERATE_16000,
            stt.AudioSampleRates.SAMPLERATE_44100,
            stt.AudioSampleRates.SAMPLERATE_48000,
        ]

    @property
    @override
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return the accepted channel layouts."""
        return [stt.AudioChannels.CHANNEL_MONO, stt.AudioChannels.CHANNEL_STEREO]

    @override
    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Collect the utterance, then transcribe it in one request."""
        audio = bytearray()
        async for chunk in stream:
            audio.extend(chunk)
        data = bytes(audio)
        if metadata.format is stt.AudioFormats.WAV:
            # Assist sends headerless PCM under the WAV label.
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(metadata.channel.value)
                wav.setsampwidth(metadata.bit_rate.value // 8)
                wav.setframerate(metadata.sample_rate.value)
                wav.writeframes(data)
            data = buffer.getvalue()

        current_service.set(self.subentry.title)
        try:
            text = await self.entry.runtime_data.client.transcribe(
                data,
                f"audio.{metadata.format.value}",
                self.model_id,
                language=metadata.language.split("-")[0],
                prompt=self.subentry.data.get(CONF_PROMPT) or None,
            )
        except BladeError as err:
            LOGGER.warning("Blade transcription failed: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        if not text:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)
