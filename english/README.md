# English Edition

This folder is the **English-prompt edition** of the pipeline: the six prompt-builder
functions in `openai_utils.py` instruct the model in English. The model's *output* is
still Traditional-Chinese Markdown tables — the parsers key off those exact Chinese
tokens — so results are fully interchangeable with the `../chinese/` edition, which
is identical apart from those six prompt functions and a few English log messages.

- **Full usage guide:** the repository root [`../README.md`](../README.md)
  (run its commands from inside this folder).
- **Prompt documentation:** [`PROMPTS_EN.md`](PROMPTS_EN.md) — every prompt in
  English, plus the list of Chinese output tokens that must never be changed.
