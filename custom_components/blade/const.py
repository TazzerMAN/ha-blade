"""Constants for the Blade integration."""

import logging
from typing import Final

from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.helpers import llm

DOMAIN: Final = "blade"
LOGGER = logging.getLogger(__package__)

SUBENTRY_CONVERSATION: Final = "conversation"
SUBENTRY_AI_TASK: Final = "ai_task_data"
SUBENTRY_EVALUATE: Final = "ai_task_evaluate"
SUBENTRY_STT: Final = "stt"
SUBENTRY_TTS: Final = "tts"

CONF_RECOMMENDED: Final = "recommended"
CONF_TEMPERATURE: Final = "temperature"
CONF_MAX_TOKENS: Final = "max_tokens"
CONF_REASONING_EFFORT: Final = "reasoning_effort"
CONF_WEB_SEARCH: Final = "web_search"
CONF_WEB_FETCH: Final = "web_fetch"
CONF_CODE_INTERPRETER: Final = "code_interpreter"
# Extended OpenAI Conversation compatible tools, as YAML.
CONF_FUNCTIONS: Final = "functions"
CONF_VISION: Final = "vision"
CONF_IMAGES_ENABLED: Final = "images_enabled"
CONF_IMAGE_MODEL: Final = "image_model"
CONF_IMAGE_SIZE: Final = "image_size"
CONF_VOICE: Final = "voice"
CONF_STREAMING: Final = "streaming"
# One-shot flag: build the "Blade" Assist pipeline after the first setup.
CONF_CREATE_PIPELINE: Final = "create_pipeline"
PIPELINE_NAME: Final = "Blade"

CONF_OCR_ENABLED: Final = "ocr_enabled"
CONF_OCR_MODEL: Final = "ocr_model"
CONF_TRANSLATE_ENABLED: Final = "translate_enabled"
CONF_TRANSLATE_MODEL: Final = "translate_model"
CONF_FORECAST_ENABLED: Final = "forecast_enabled"
CONF_FORECAST_MODEL: Final = "forecast_model"
# Estimated monthly spend cap in EUR; 0 means no cap.
CONF_MONTHLY_BUDGET: Final = "monthly_budget"

KIND_CHAT: Final = "chat"
KIND_IMAGE: Final = "image"
KIND_DECISION: Final = "decision"
KIND_TRANSCRIPTION: Final = "transcription"
KIND_SPEECH: Final = "speech"
KIND_FORECAST: Final = "forecast"

REASONING_EFFORTS: Final = ["none", "low", "medium", "high"]
IMAGE_SIZES: Final = ["512x512", "768x768", "1024x1024", "1024x768", "768x1024"]

RECOMMENDED_CHAT_MODEL: Final = "qwen3.6-35b-a3b"
RECOMMENDED_AI_TASK_MODEL: Final = "gemma-4-31b"
RECOMMENDED_IMAGE_MODEL: Final = "flux-2-klein"
RECOMMENDED_IMAGE_SIZE: Final = "1024x1024"
RECOMMENDED_DECISION_MODEL: Final = "clef-flash"
RECOMMENDED_STT_MODEL: Final = "voxtral-mini-3b"
RECOMMENDED_TTS_MODEL: Final = "kyutai-tts-1.6b-en-fr"
RECOMMENDED_OCR_MODEL: Final = "ovisocr3-0.8b"
RECOMMENDED_TRANSLATE_MODEL: Final = "hy-mt2-7b"
RECOMMENDED_FORECAST_MODEL: Final = "timestral-28m"
RECOMMENDED_TEMPERATURE: Final = 0.7
RECOMMENDED_MAX_TOKENS: Final = 3000
RECOMMENDED_REASONING_EFFORT: Final = "none"

# Audio models served under kind "chat" that still answer /audio/transcriptions.
EXTRA_STT_MODELS: Final = ("voxtral-mini-3b", "gladia*", "solaria*")

# Voices documented per speech model; any other id can still be typed in.
SPEECH_VOICES: Final[dict[str, dict[str, str]]] = {
    "kyutai-tts-1.6b-en-fr": {
        "unmute-prod-website/developpeuse-3.wav": "Développeuse (FR)",
        "unmute-prod-website/developer-1.mp3": "Developer (EN)",
    },
    "kokoro-82m": {
        voice: voice
        for voice in (
            "af_heart",
            "af_bella",
            "af_nicole",
            "af_sarah",
            "af_sky",
            "am_adam",
            "am_michael",
            "am_fenrir",
            "am_puck",
            "bf_emma",
            "bf_isabella",
            "bm_george",
            "bm_lewis",
        )
    },
}
SPEECH_LANGUAGES: Final[dict[str, list[str]]] = {
    "kyutai-tts-1.6b-en-fr": ["fr-FR", "en-US", "en-GB"],
    "kokoro-82m": ["en-US", "en-GB"],
}
# Models whose voices only live in the streaming WebSocket.
STREAMING_SPEECH_MODELS: Final = frozenset({"kyutai-tts-1.6b-en-fr"})

DEFAULT_STT_PROMPT: Final = (
    "The following conversation is a smart home user talking to Home Assistant."
)

# Home Assistant ships its default instructions in English only.
DEFAULT_PROMPTS: Final[dict[str, str]] = {
    "fr": (
        "Tu es un assistant vocal pour Home Assistant.\n"
        "Réponds avec exactitude aux questions sur le monde.\n"
        "Réponds simplement et directement, en texte brut."
    ),
}


def default_prompt(language: str) -> str:
    """Return the default agent instructions in the user's language."""
    base = language.split("-", maxsplit=1)[0].lower()
    return DEFAULT_PROMPTS.get(base, llm.DEFAULT_INSTRUCTIONS_PROMPT)


RECOMMENDED_CONVERSATION_OPTIONS: Final = {
    CONF_RECOMMENDED: True,
    "model": RECOMMENDED_CHAT_MODEL,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
    CONF_WEB_SEARCH: False,
    CONF_WEB_FETCH: False,
    CONF_CODE_INTERPRETER: False,
}
RECOMMENDED_AI_TASK_OPTIONS: Final = {
    "model": RECOMMENDED_AI_TASK_MODEL,
    CONF_IMAGES_ENABLED: True,
    CONF_IMAGE_MODEL: RECOMMENDED_IMAGE_MODEL,
    CONF_IMAGE_SIZE: RECOMMENDED_IMAGE_SIZE,
}
RECOMMENDED_EVALUATE_OPTIONS: Final = {"model": RECOMMENDED_DECISION_MODEL}
RECOMMENDED_STT_OPTIONS: Final = {"model": RECOMMENDED_STT_MODEL}
RECOMMENDED_TTS_OPTIONS: Final = {
    "model": RECOMMENDED_TTS_MODEL,
    CONF_VOICE: "unmute-prod-website/developpeuse-3.wav",
    CONF_STREAMING: True,
}
DEFAULT_ENTRY_OPTIONS: Final = {
    CONF_OCR_ENABLED: True,
    CONF_OCR_MODEL: RECOMMENDED_OCR_MODEL,
    CONF_TRANSLATE_ENABLED: True,
    CONF_TRANSLATE_MODEL: RECOMMENDED_TRANSLATE_MODEL,
    CONF_FORECAST_ENABLED: True,
    CONF_FORECAST_MODEL: RECOMMENDED_FORECAST_MODEL,
    CONF_MONTHLY_BUDGET: 0.0,
}

# Default subentry titles: brand first, like "Google AI TTS" in the same lists.
DEFAULT_NAMES: Final[dict[str, str]] = {
    SUBENTRY_CONVERSATION: "Blade Conversation",
    SUBENTRY_AI_TASK: "Blade AI Task",
    SUBENTRY_EVALUATE: "Blade Evaluation",
    SUBENTRY_STT: "Blade STT",
    SUBENTRY_TTS: "Blade TTS",
}


def default_name(subentry_type: str) -> str:
    """Return the default title of a subentry."""
    return DEFAULT_NAMES[subentry_type]


# Preference order per option, best first, from measurements on the real API.
# Glob patterns let a provider's future models win as soon as an account gets them.
RECOMMENDED_MODELS: Final[dict[str, tuple[str, ...]]] = {
    # Tool calls proved reliable with long prompts (24/24, 1.3-2.5 s).
    "conversation": ("qwen3.6-35b-a3b", "gemma-4-26b-a4b", "hypernova-60b"),
    # Best French writing among vision models.
    "ai_task": ("gemma-4-31b", "qwen3.8-27b", "qwen3-vl-30b-a3b"),
    # qwen-image-2.1 draws better but sleeps: ~150 s on a first call.
    "image": ("flux-2-klein", "sdxl-lightning"),
    "evaluate": ("clef-flash",),
    # Gladia first once enabled; Voxtral as fast as Whisper on average, steadier.
    "stt": ("gladia*", "solaria*", "voxtral-mini-3b", "whisper-large-v3-turbo"),
    "tts": ("kyutai-tts-1.6b-en-fr", "kokoro-82m"),
    # Follows free instructions; paddleocr-vl only reads well on its own "OCR:".
    "ocr": ("ovisocr3-0.8b", "deepseek-ocr-2", "lightonocr-3-4b"),
    "translate": ("hy-mt2-7b", "hy-mt2-30b-a3b", "hy-mt2-1.8b"),
    "forecast": ("timestral-28m", "timestral-123m", "timestral-5m"),
}

MAX_TOOL_ITERATIONS: Final = 10
