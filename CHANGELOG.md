# Changelog

## 1.1.0

- AI tasks check structured answers against the requested structure. When the model misses it, Blade gets one correction naming the problem; if the second answer still does not fit, the error says what is wrong.
- A model that is still starting on Blade's side now gives a clear message ("try again in a minute") instead of a raw API error.
- The recommended OCR model is now `ovisocr3-0.8b`, followed by `deepseek-ocr-2` and `lightonocr-3-4b`. Existing setups keep the model they use.
- `qwen-image-2.1` leaves the recommended image models: it takes about two and a half minutes to answer when it has been idle. `flux-2-klein` and `sdxl-lightning` stay.

## 1.0.0

First release.

- Conversation agents for Assist, with streamed answers, Home Assistant control, optional reasoning, and optional Blade web search, web page reading and Python code execution (off by default).
- Custom functions in the Extended OpenAI Conversation format (`native` execute_service, `script`, `template`) and the `exposed_entities` prompt variable.
- AI tasks: text and structured data, camera snapshots and photos as input, image generation.
- Evaluation (`ai_task.evaluate`) with calibrated probabilities.
- Speech-to-text and text-to-speech, with speech streamed while the agent is still writing.
- Actions: `blade.ocr`, `blade.translate`, `blade.forecast`, each switchable.
- One-step setup: models picked from the account's catalog, refreshed every hour, and an optional ready-made Blade voice assistant.
- Estimated spend sensors, monthly spending cap and account status with repairs.
- English and French.
