# Changelog

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
