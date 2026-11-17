"""Text-to-speech with Blade speech models, streamed when the model allows it."""

from __future__ import annotations

import io
import re
import wave
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, override

from homeassistant.components import tts
from homeassistant.core import callback

from .api import BladeError, current_service
from .const import (
    CONF_STREAMING,
    CONF_VOICE,
    SPEECH_LANGUAGES,
    SPEECH_VOICES,
    STREAMING_SPEECH_MODELS,
    SUBENTRY_TTS,
)
from .entity import BladeEntity, blade_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import BladeConfigEntry

PARALLEL_UPDATES = 0
_SENTENCE_END = re.compile(r"(?<=[.!?…:;])\s+|\n+")
# Utterances shorter than this are merged with the next one to avoid audible seams.
_MIN_UTTERANCE = 40


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BladeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one TTS entity per TTS subentry."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type == SUBENTRY_TTS:
            entity = (
                BladeStreamingTTSEntity(config_entry, subentry)
                if subentry.data.get(CONF_STREAMING)
                and subentry.data["model"] in STREAMING_SPEECH_MODELS
                else BladeTTSEntity(config_entry, subentry)
            )
            async_add_entities([entity], config_subentry_id=subentry_id)


class BladeTTSEntity(tts.TextToSpeechEntity, BladeEntity):
    """Synthesize complete WAV files."""

    _attr_supported_options = [tts.ATTR_VOICE]
    # The TTS component refuses an engine without a name of its own.
    _attr_has_entity_name = False

    def __init__(self, entry: BladeConfigEntry, subentry: ConfigSubentry) -> None:
        """Pick languages and voices from the configured model."""
        super().__init__(entry, subentry)
        self._attr_name = subentry.title
        languages = SPEECH_LANGUAGES.get(self.model_id, ["en-US"])
        self._attr_supported_languages = languages
        self._attr_default_language = languages[0]
        self._voices = [
            tts.Voice(voice_id, name)
            for voice_id, name in SPEECH_VOICES.get(self.model_id, {}).items()
        ]
        configured = subentry.data.get(CONF_VOICE)
        if configured and configured not in {v.voice_id for v in self._voices}:
            self._voices.insert(0, tts.Voice(configured, configured))

    @callback
    @override
    def async_get_supported_voices(self, language: str) -> list[tts.Voice]:
        """Return the documented voices of the model."""
        return self._voices

    @property
    @override
    def default_options(self) -> Mapping[str, Any]:
        """Use the voice chosen in the subentry."""
        voice = self.subentry.data.get(CONF_VOICE) or (
            self._voices[0].voice_id if self._voices else None
        )
        return {tts.ATTR_VOICE: voice} if voice else {}

    @override
    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> tts.TtsAudioType:
        """Return the whole message as one WAV file."""
        current_service.set(self.subentry.title)
        try:
            audio = await self.entry.runtime_data.client.speech(
                message, self.model_id, options[tts.ATTR_VOICE]
            )
        except BladeError as err:
            raise blade_error(err, self.model_id) from err
        return "wav", audio


class BladeStreamingTTSEntity(BladeTTSEntity):
    """Speak while the conversation agent is still writing."""

    @override
    async def async_stream_tts_audio(
        self, request: tts.TTSAudioRequest
    ) -> tts.TTSAudioResponse:
        """Feed sentences to the speech WebSocket as soon as they are complete."""
        options = {**self.default_options, **request.options}
        try:
            stream = await self.entry.runtime_data.client.speech_stream(
                split_sentences(request.message_gen),
                self.model_id,
                options[tts.ATTR_VOICE],
            )
        except BladeError as err:
            raise blade_error(err, self.model_id) from err

        async def wav_stream() -> AsyncGenerator[bytes]:
            # A zero-length header tells players the size is unknown.
            current_service.set(self.subentry.title)
            yield wav_header(stream.sample_rate, stream.channels)
            try:
                async for chunk in stream.chunks:
                    yield chunk
            except BladeError as err:
                raise blade_error(err, self.model_id) from err

        return tts.TTSAudioResponse("wav", wav_stream())


def wav_header(sample_rate: int, channels: int) -> bytes:
    """Build a 16-bit PCM WAV header with no frames."""
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
        return buffer.getvalue()


async def split_sentences(chunks: AsyncGenerator[str]) -> AsyncGenerator[str]:
    """Regroup streamed text into complete sentences."""
    pending = ""
    async for chunk in chunks:
        pending += chunk
        parts = _SENTENCE_END.split(pending)
        pending = parts.pop()
        utterance = ""
        for part in parts:
            utterance = f"{utterance} {part}".strip()
            if len(utterance) >= _MIN_UTTERANCE:
                yield utterance
                utterance = ""
        # Keep the separator: the next chunk may start right after it.
        pending = f"{utterance} {pending}" if utterance else pending
    if pending.strip():
        yield pending.strip()
