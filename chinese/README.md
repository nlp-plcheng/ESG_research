# ESG 承諾跨年度驗證流程

針對台灣上市櫃公司的 ESG 報告書（民國年），自動跑完以下流程：

**擷取承諾 → 跨年度整合 → 逐年驗證是否兌現 → 匯整成 JSON**

想解決的問題：一條在 X 年報告書提出、目標訂在 Y 年的承諾，有可能在中間某一年 Z 就提早做到，而相關證據會散落在不同年度的報告裡。這套流程用「重疊分頁」(overlapping PDF chunks) 一年一年掃過報告，替每一條承諾判定「已達成 / 部分達成 / 未提及」，並留下逐年的變化。

> 這是**原始中文版**（prompt 為中文）。英文主說明在 repo 根目錄的 `../README.md`；給國外團隊的英文 prompt 版在 `../english/`（僅 prompt 指令改為英文，模型輸出的表格與本版完全相同）。

---

## 1. 環境需求

- Python 3.8+
- 套件：`openai`、`python-dotenv`、`pypdf`

```bash
pip install -r requirements.txt
```

在此資料夾（或其任一上層目錄）放一個 `.env`（可參考 `.env.example`）：

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL_NAME=gpt-5.4
```

其他可以微調的參數（都有預設值，通常不用改）：

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `OPENAI_PAGES_PER_CHUNK` | 20 | 每個 PDF 分頁區塊的頁數 |
| `OPENAI_CHUNK_OVERLAP` | 2 | 相鄰區塊重疊的頁數 |
| `OPENAI_VERIFY_MAX_TOKENS` | 32768 | 單次驗證輸出的 token 上限 |
| `OPENAI_MAX_CLUES_PER_CHUNK` | 3 | 單一區塊每條承諾可列的相關線索數 |
| `OPENAI_MAX_CLUES` | 6 | 最終結果每條承諾保留的相關線索數 |
| `OPENAI_MAX_RETRIES` | 5 | 單次 API 呼叫的重試次數 |
| `OPENAI_REQUEST_TIMEOUT` | 300 | 單次 API 呼叫的逾時秒數 |

---

## 2. 檔案架構

一家公司一個資料夾，檔名為民國年：

```
pdf/
  台積電/
    109.pdf
    110.pdf
    111.pdf
  聯電/
    110.pdf
    ...
```

---

## 3. 執行

跑單一公司（自動偵測該公司 pdf/ 底下所有年度）：

```bash
python openai_run_company.py --company 台積電 --pdf_dir pdf --result_dir result
```

跑 `pdf/` 底下所有公司：

```bash
python openai_run_all.py --pdf_dir pdf --result_dir result
```

最後把逐年結果匯整成每家公司一份 `summary.json`：

```bash
python openai_build_summary_json.py --result_dir result
```

接著要做人工複核的話，進 `annotation_platform/`（人工複核平台，預設讀本資料夾的
`result/` 與 `pdf/`；詳見 [`annotation_platform/README.md`](annotation_platform/README.md)）：

```bash
cd annotation_platform
bash run.sh setup && bash run.sh test   # 先本機試用
bash run.sh prod-init                   # 再編輯 env.prod.sh，然後 bash launch.sh
```

---

## 4. 流程

| 階段 | 程式 | 動作 | 產出 |
|---|---|---|---|
| 1. 擷取 | `openai_get_promise.py` | 讀某一年的報告，把「目標年份在未來」的承諾挑出來 | `result/{公司}/{年}/promise.md` |
| 2. 整合 | `openai_consolidate_promises.py` | 把各年的承諾依「目標年份」分組、語意去重，並統一用詞 | `result/{公司}/promise/{目標年}.md` |
| 3. 驗證 | `openai_check_promise.py` | 拿某一年的報告，比對還在追蹤中的承諾，判定達成狀態 | `result/{公司}/{年}/check_result.md` |
| 4. 匯整 | `openai_build_summary_json.py` | 把每年的 check_result.md 匯整成逐年時間軸 | `result/{公司}/summary.json` |

`openai_run_company.py` 會照順序跑階段 1 → 2 → 3；`openai_run_all.py` 則會對每家公司各跑一次。階段 4（`build_summary_json`）通常留到最後再單獨跑一次。

### 順序

- 階段 1 得先把**所有年度**都跑完，階段 2 才有辦法把每一年的承諾併在一起。
- 階段 2 必須排在階段 3 前面，因為驗證時讀的是階段 2 產生、已經「統一用詞」的 `promise/{目標年}.md`——這樣同一條承諾在不同年度才會用一致的文字，提早兌現的年份和目標年份也才對得起來。

### 驗證的範圍

階段 3 在驗證第 Y 年時，只挑「**發布年份 < Y 且 目標年份 ≥ Y**」的承諾：

- 發布年份 < Y：只驗證更早的報告就宣告過的承諾（當年才第一次提出的承諾，要隔年才開始查核）。
- 目標年份 ≥ Y：連同「目標在更晚年份」的承諾一起看，才能抓到提早兌現的案例。

### 可重跑（resume-friendly）

每個階段都是「輸出已存在就跳過」：

- `promise.md` 已存在 → 跳過該年擷取
- `promise/{目標年}.md` 已存在 → 跳過該目標年整合
- `check_result.md` 已存在 → 跳過該年驗證

想重做某一步，把對應的輸出檔刪掉再重跑就好。也可以用 `--skip_get` / `--skip_consolidate` / `--skip_check` 跳過整個階段。

---

## 5. 程式內容

| 程式 | 說明 | 主要參數 |
|---|---|---|
| `openai_run_all.py` | 批次跑所有公司 | `--pdf_dir` `--result_dir` `--companies` `--skip_*` |
| `openai_run_company.py` | 單一公司跑完 1→2→3 | `--company` `--pdf_dir` `--result_dir` `--years` `--skip_*` |
| `openai_get_promise.py` | 階段 1：擷取承諾 | `--company` `--year` `--pdf_dir` `--result_dir` |
| `openai_consolidate_promises.py` | 階段 2：整合承諾 | `--company` `--result_dir` `--years` |
| `openai_check_promise.py` | 階段 3：驗證承諾 | `--company` `--year` `--pdf_dir` `--result_dir` |
| `openai_build_summary_json.py` | 階段 4：匯整 summary.json | `--company` `--result_dir` |
| `openai_utils.py` | 共用工具（設定、API 呼叫、prompt、表格解析、跨年度彙整） | —（被上面各程式 import） |

---

## 6. 輸出資料格式

### promise.md / promise/{目標年}.md（4 欄）

```
| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
```

- **承諾**：精簡核心敘述（不含年份）
- **發布年份 / 目標年份**：民國年
- **承諾來源頁碼**：該承諾出現的報告印刷頁碼

### check_result.md（8 欄）

```
| 承諾 | 發布年份 | 目標年份 | 達成狀態 | 證據 | 承諾來源頁碼 | 證據來源頁碼 | 其他相關/可疑證據 |
```

- **達成狀態**：`已達成` / `部分達成` / `未提及`
- **證據**：從當年報告精確摘錄的原文（無則 `無資料`）
- **其他相關/可疑證據**：供人工複核的相關片段，多條以 `<br>` 分隔

### summary.json（每家公司一份）

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
      "status": [                         // 逐年時間軸
        { "year": 110, "status": "未提及",   "evidence": null,   "evidence_page": null, "clues": ["…（第12頁）"] },
        { "year": 111, "status": "部分達成", "evidence": "…",    "evidence_page": "88", "clues": [] }
      ]
    }
  ]
}
```

- **final_status**：綜合整條時間軸後的最終判定（`已達成` > `部分達成` > `未提及`）
- **status**：逐年（發布年+1 ~ 目標年，且該年有報告）的判定、證據與相關線索

---

## 7. 備註

- repo 根目錄的 `result/` 是實際跑出的範例結果；輸入 `pdf/` 不隨 repo 發布，請依第 2 節自行放置。
- 整個流程對每份 PDF 只做一次「重疊分頁」掃描，整合與匯整階段只讀硬碟上的 Markdown，不會重複讀 PDF，藉此省下 token。
