# ESG Cross-Year Commitment Verification Pipeline

For Taiwanese listed-company ESG reports (dated in ROC / 民國 years), this pipeline
runs the whole flow automatically:

**Extract commitments → consolidate across years → verify year-by-year whether they
were delivered → roll up into JSON**

The problem it solves: a commitment made in year X's report with a target of year Y
may actually be delivered *early*, in some intermediate year Z — and the evidence is
scattered across different years' reports. The pipeline scans each report in
overlapping PDF chunks, one year at a time, judges every commitment as
`已達成` / `部分達成` / `未提及` (achieved / partially achieved / not mentioned), and
keeps the year-by-year trace.

> **ROC year = Gregorian year − 1911** (e.g. ROC 109 = 2020, ROC 113 = 2024).

## Repository layout

| Path | What it is |
|---|---|
| `english/` | The pipeline with **English prompt instructions** — for international teams. `english/PROMPTS_EN.md` documents all six prompts and the Chinese output tokens that must never change. |
| `chinese/` | The pipeline with the **original Chinese prompts** (原始中文版), plus the full Chinese guide `chinese/README.md`. |
| `result/` | Sample outputs from real runs: `result/{company}/{year}/promise.md` + `check_result.md`, `result/{company}/promise/{target}.md`, and one `summary.json` per company. |
| `english/annotation_platform/`, `chinese/annotation_platform/` | Step 5: the **human verification platform** for each edition (Flask + SQLite; annotators check the `summary.json` verdicts year by year). Each has its own README with the required `pdf/` + `result/` layout and the configuration. |

The two editions are **identical except the six prompt-builder functions in
`openai_utils.py`** (plus a few English log messages in `english/`). Both demand
the same output schema from the model —
Traditional-Chinese Markdown tables — because the table parsers key off exact
Chinese tokens (the three status labels, the `無資料` "no data" placeholder, the
column headers). Results from the two editions are interchangeable, and `result/`
looks the same whichever edition produced it.

---

## 1. Setup

- Python 3.8+
- Packages: `openai`, `python-dotenv`, `pypdf`

```bash
cd english        # or: cd chinese
pip install -r requirements.txt
```

Put a `.env` in the edition folder you run from (or any parent directory; see
`.env.example`):

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL_NAME=gpt-5.4
```

Optional tuning knobs (all have defaults; usually no need to touch them):

| Env var | Default | Meaning |
|---|---|---|
| `OPENAI_PAGES_PER_CHUNK` | 20 | Pages per PDF chunk |
| `OPENAI_CHUNK_OVERLAP` | 2 | Overlapping pages between adjacent chunks |
| `OPENAI_VERIFY_MAX_TOKENS` | 32768 | Output-token cap for one verification pass |
| `OPENAI_MAX_CLUES_PER_CHUNK` | 3 | Max related clues per commitment within one chunk |
| `OPENAI_MAX_CLUES` | 6 | Max related clues kept per commitment in the final result |
| `OPENAI_MAX_RETRIES` | 5 | Retries per API call |
| `OPENAI_REQUEST_TIMEOUT` | 300 | Per-call timeout (seconds) |

## 2. Input layout

Inside the edition folder you run from, one folder per company; file names are the
ROC report year:

```
pdf/
  <company>/
    109.pdf      # ROC 109 = 2020
    110.pdf
    111.pdf
  <another-company>/
    110.pdf
    ...
```

The company folder name is used verbatim as `--company` and in the output paths, so
it can be any string (Chinese names such as `台積電` are fine).

## 3. Running

Run these from inside `english/` or `chinese/`.

Run a single company (auto-discovers every year under that company's `pdf/` folder):

```bash
python openai_run_company.py --company <company> --pdf_dir pdf --result_dir result
```

Run every company under `pdf/`:

```bash
python openai_run_all.py --pdf_dir pdf --result_dir result
```

Finally, roll the year-by-year results into one `summary.json` per company:

```bash
python openai_build_summary_json.py --result_dir result
```

## 4. Pipeline

| Stage | Program | What it does | Output |
|---|---|---|---|
| 1. Extract | `openai_get_promise.py` | Read one year's report; pull out the commitments whose target year is in the future | `result/{company}/{year}/promise.md` |
| 2. Consolidate | `openai_consolidate_promises.py` | Group each year's commitments by target year, dedupe by meaning, and unify the wording | `result/{company}/promise/{target}.md` |
| 3. Verify | `openai_check_promise.py` | Take one year's report, compare it against the still-tracked commitments, and judge the status | `result/{company}/{year}/check_result.md` |
| 4. Roll up | `openai_build_summary_json.py` | Fold every year's `check_result.md` into one year-by-year timeline | `result/{company}/summary.json` |

`openai_run_company.py` runs stages 1 → 2 → 3 in order; `openai_run_all.py` calls it
once per company. Stage 4 (`build_summary_json`) is normally run once at the very end.

### Why the order matters

- Stage 1 must finish for **all years** before Stage 2 can fold every year's
  commitments together.
- Stage 2 must run before Stage 3, because verification reads the "unified-wording"
  `promise/{target}.md` that Stage 2 produces — so the same commitment reads
  identically across years, and an early fulfilment lines up with its target year.

### What Stage 3 verifies

When verifying year Y, Stage 3 only picks commitments with
**declared year (`發布年份`) < Y AND target year (`目標年份`) ≥ Y**:

- declared < Y: only verify commitments already declared in an *earlier* report (a
  commitment first stated in year Y itself isn't checked until the following year).
- target ≥ Y: also look at commitments targeting *later* years, so early fulfilments
  get caught (their evidence shows up before the target year).

### Resume-friendly

Every stage is "skip if the output already exists":

- `promise.md` exists → skip that year's extraction
- `promise/{target}.md` exists → skip that target year's consolidation
- `check_result.md` exists → skip that year's verification

To redo a step, delete the matching output file and re-run. You can also skip a whole
stage with `--skip_get` / `--skip_consolidate` / `--skip_check`.

## 5. Programs

| Program | What it is | Key args |
|---|---|---|
| `openai_run_all.py` | Batch-run every company | `--pdf_dir` `--result_dir` `--companies` `--skip_*` |
| `openai_run_company.py` | Run stages 1 → 2 → 3 for one company | `--company` `--pdf_dir` `--result_dir` `--years` `--skip_*` |
| `openai_get_promise.py` | Stage 1: extract commitments | `--company` `--year` `--pdf_dir` `--result_dir` |
| `openai_consolidate_promises.py` | Stage 2: consolidate commitments | `--company` `--result_dir` `--years` |
| `openai_check_promise.py` | Stage 3: verify commitments | `--company` `--year` `--pdf_dir` `--result_dir` |
| `openai_build_summary_json.py` | Stage 4: roll up `summary.json` | `--company` `--result_dir` |
| `openai_utils.py` | Shared library (config, API calls, prompts, table parsing, cross-year rollup) | — (imported by the above) |

See `english/PROMPTS_EN.md` for the full English documentation of every prompt.

## 6. Output formats

These are exactly what the files under `result/` contain. Column headers and status
values are Chinese by design; English glosses in parentheses.

### promise.md / promise/{target}.md (4 columns)

```
| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
```

- **承諾** (commitment): concise core statement (no year in the text)
- **發布年份 / 目標年份** (declared year / target year): ROC year
- **承諾來源頁碼** (source page): printed page where the commitment appears

### check_result.md (8 columns)

```
| 承諾 | 發布年份 | 目標年份 | 達成狀態 | 證據 | 承諾來源頁碼 | 證據來源頁碼 | 其他相關/可疑證據 |
```

(commitment | declared year | target year | status | evidence | source page |
evidence page | other related/suspicious evidence)

- **達成狀態** (status): `已達成` (achieved) / `部分達成` (partial) / `未提及` (not mentioned)
- **證據** (evidence): verbatim excerpt from that year's report (`無資料` = none)
- **其他相關/可疑證據** (other related/suspicious evidence): snippets for human review,
  multiple joined by `<br>`

### summary.json (one per company)

```jsonc
{
  "company": "台積電",
  "total_promises": 42,
  "status_counts": { "已達成": 10, "部分達成": 20, "未提及": 12 },
  "promises": [
    {
      "commitment": "再生能源使用比例達 100%",
      "declared_year": 109,
      "target_year": 113,
      "source_page": "45",
      "final_status": "部分達成",
      "status": [                         // year-by-year timeline
        { "year": 110, "status": "未提及",   "evidence": null,   "evidence_page": null, "clues": ["…（第12頁）"] },
        { "year": 111, "status": "部分達成", "evidence": "…",    "evidence_page": "88", "clues": [] }
      ]
    }
  ]
}
```

- **final_status**: the whole timeline folded into one verdict
  (`已達成` > `部分達成` > `未提及`)
- **status**: the per-year timeline (declared year + 1 .. target year, for the years
  that actually have a report), each with its verdict, evidence and related clues

## 7. Notes

- Each PDF is scanned only once, in overlapping chunks; the consolidate and roll-up
  stages read only the Markdown already on disk — they never re-read the PDF — which
  keeps token usage down.
- `result/` in this repo is a sample produced by real runs; input PDFs are not
  distributed with the repo (put your own under `pdf/` per §2). Per-edition `pdf/`
  and `result/` folders created at runtime are git-ignored.
- If the model ever drifts from the Chinese output schema, the built-in table
  validators reject the malformed response and retry; the token contract is spelled
  out in `english/PROMPTS_EN.md`. After `OPENAI_MAX_RETRIES` failed attempts a stage
  stops with a non-zero exit and writes nothing for that year — it never records a
  failed call as 未提及 / 無資料 — and `openai_run_company.py` / `openai_run_all.py`
  exit non-zero too, so a partial run is never mistaken for a finished one.

---

# 中文導覽

本 repo 是同一套「ESG 承諾跨年度驗證」pipeline 的雙語打包：

- **`chinese/`** — 原始中文版（prompt 為中文），完整中文說明請見
  [`chinese/README.md`](chinese/README.md)，內容與上方英文指南對應。
- **`english/`** — 給國外團隊的英文版：僅 `openai_utils.py` 內六個 prompt 的
  「指令」與少數 log 訊息改為英文，其餘程式碼與中文版完全相同；模型輸出仍是相同格式的中文表格
  （解析程式認的是固定中文欄位與標籤）。prompt 對照文件：
  [`english/PROMPTS_EN.md`](english/PROMPTS_EN.md)。
- **`result/`** — 實際跑出的範例結果（`promise.md`、`check_result.md`、
  `summary.json`），兩版產出格式相同。
- **`chinese/annotation_platform/`、`english/annotation_platform/`** — 第 5 步：人工複核平台
  （標註者逐年檢查 `summary.json` 的判定），各有自己的 README 說明檔案結構與設定。

執行方式：進入 `chinese/` 或 `english/`，把報告放到 `pdf/<公司>/<民國年>.pdf`，
依上方 §3 的指令執行即可。
