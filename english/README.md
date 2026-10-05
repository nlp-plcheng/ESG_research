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
- **Human verification (step 5):** [`annotation_platform/`](annotation_platform/README.md)
  — an English web platform where annotators check the `summary.json` verdicts
  year by year. It reads `result/<company>/summary.json` and `pdf/<company>/<year>.pdf`
  from this folder by default (`ESG_RESULT_DIR` / `ESG_PDF_DIR` override that), so
  once `openai_build_summary_json.py` has run:

  ```bash
  cd annotation_platform
  bash run.sh setup && bash run.sh test      # try it locally
  bash run.sh prod-init                      # then edit env.prod.sh and: bash launch.sh
  ```
