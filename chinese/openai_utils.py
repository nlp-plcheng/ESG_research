"""Shared utilities for the OpenAI PDF-file ESG promise-verification pipeline.

Holds the runtime config (model names, chunk sizes, token/retry knobs), the
OpenAI chat caller with bounded retry + response validation, the overlapping
PDF-chunk reader, all extraction / verification / judge-consolidation prompts,
the Markdown-table parsers and writers used as the data interchange format, and
the pure-code cross-year aggregation that folds each promise into a per-year
timeline.

Imported by openai_get_promise.py, openai_consolidate_promises.py,
openai_check_promise.py and openai_build_summary_json.py.
"""

import os
import io
import re
import time
import base64
from typing import Callable, Dict, List, Optional, Tuple
from dotenv import load_dotenv
from openai import OpenAI

# pypdf powers IterPdfChunks (the whole pipeline reads each report as overlapping
# page chunks). Soft import with a PyPDF2 fallback; IterPdfChunks raises a clear
# error if neither is installed.
try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
    try:
        from PyPDF2 import PdfReader, PdfWriter
    except ImportError:
        PdfReader = None
        PdfWriter = None


load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL_NAME", "gpt-5.4")

VISION_MODELS = [OPENAI_MODEL]

JUDGE_MODEL = OPENAI_MODEL


def CreateOpenaiClient() -> OpenAI:
    """Creates and returns the OpenAI API client."""
    if not OPENAI_API_KEY:
        raise ValueError("Environment variable OPENAI_API_KEY is not set.")
    return OpenAI(api_key=OPENAI_API_KEY)


# =====================================================================
# Promise pipeline (PDF-file based, OpenAI Chat Completions)
#
# Powers openai_get_promise.py / openai_check_promise.py. Instead of one image
# per page, the report is sent as overlapping PDF chunks attached directly as
# files in the chat (default 20 pages per chunk, 2-page overlap: 1-20, 19-38,
# 37-56, ...). Self-contained: this module has no cross-pipeline imports.
# =====================================================================

# Chunking + call config (env-overridable).
PAGES_PER_CHUNK = int(os.getenv("OPENAI_PAGES_PER_CHUNK", "20"))
CHUNK_OVERLAP = int(os.getenv("OPENAI_CHUNK_OVERLAP", "2"))
REQUEST_TIMEOUT_S = float(os.getenv("OPENAI_REQUEST_TIMEOUT", "300"))
MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "5"))
# Output-token budget for one verify pass. The verify table now emits only the
# commitments a chunk actually evidences or has clues for (the rest default to
# 未提及 during aggregation), so the output is far smaller than the canonical list.
# A chunk that evidences many commitments at once can still be large, so this is
# generous and OPENAI_VERIFY_MAX_TOKENS can raise it further up to the model's
# output cap. Extract/judge passes keep their own (smaller) budgets.
VERIFY_MAX_TOKENS = int(os.getenv("OPENAI_VERIFY_MAX_TOKENS", "32768"))
# 相關線索 (suspicious / related clue) limits.
#   MAX_CLUES_PER_CHUNK:      how many clues the model may list for one
#                             commitment within a single verify pass (chunk).
#   MAX_CLUES_PER_COMMITMENT: how many to surface in the final result table
#                             after de-duplicating across all chunks/models.
# Both env-overridable.
MAX_CLUES_PER_CHUNK = int(os.getenv("OPENAI_MAX_CLUES_PER_CHUNK", "3"))
MAX_CLUES_PER_COMMITMENT = int(os.getenv("OPENAI_MAX_CLUES", "6"))


# ===============================
# PDF chunking + file attachment
# ===============================

def IterPdfChunks(pdf_path: str, pages_per_chunk: int = None, overlap: int = None):
    """Yield (start_page, end_page, pdf_bytes) for overlapping page windows.

    Pages are 1-indexed and inclusive. With the defaults (20 pages, 2 overlap)
    the windows are 1-20, 19-38, 37-56, ... (step = pages_per_chunk - overlap).
    Each pdf_bytes is a standalone PDF holding only that window's pages.
    """
    if PdfReader is None or PdfWriter is None:
        raise ImportError(
            "IterPdfChunks requires the 'pypdf' package (or PyPDF2). "
            "Install with: pip install pypdf"
        )

    size = pages_per_chunk or PAGES_PER_CHUNK
    ov = CHUNK_OVERLAP if overlap is None else overlap
    step = max(1, size - ov)

    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    start = 0  # 0-indexed
    while start < total:
        end = min(start + size, total)  # exclusive
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        yield start + 1, end, buf.getvalue()
        if end >= total:
            break
        start += step


def _PdfFilePart(pdf_bytes: bytes, filename: str) -> dict:
    """Build an OpenAI chat 'file' content part from inline base64 PDF bytes."""
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    return {
        "type": "file",
        "file": {
            "filename": filename,
            "file_data": f"data:application/pdf;base64,{b64}",
        },
    }


def _BuildPdfContent(prompt_text: str, pdf_bytes: bytes, filename: str) -> list:
    return [
        {"type": "text", "text": prompt_text},
        _PdfFilePart(pdf_bytes, filename),
    ]


# ===============================
# Response cleaning + retry caller
# ===============================

_THINK_BLOCK = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)
_OPEN_THINK = re.compile(r"<think>.*", flags=re.DOTALL | re.IGNORECASE)
_FENCE_BLOCK = re.compile(
    r"^```(?:markdown|md|json)?\s*\n(.*?)\n```\s*$",
    flags=re.DOTALL | re.IGNORECASE,
)


def _CleanResponse(text: str) -> str:
    """Strip <think> blocks and an outer ``` fence so the table parser is happy."""
    if not text:
        return ""
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _OPEN_THINK.sub("", cleaned).strip()
    fence = _FENCE_BLOCK.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    return cleaned


def CallOpenaiWithRetry(
    client: OpenAI,
    model: str,
    content,
    max_completion_tokens: int = 4096,
    validator: Optional[Callable[[str], Tuple[bool, str]]] = None,
    allow_empty: bool = True,
    label: str = "",
) -> str:
    """Chat completion with bounded retries on errors / empty / invalid output.

    `content` may be a plain string (text-only judge calls) or a list of content
    parts (text + PDF file). `allow_empty=True` accepts a clean empty response
    (extraction legitimately returns nothing when a chunk has no commitments).
    """
    tag = f"{model}" + (f" [{label}]" if label else "")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=0.0,
                max_completion_tokens=max_completion_tokens,
                timeout=REQUEST_TIMEOUT_S,
            )
            cleaned = _CleanResponse(response.choices[0].message.content or "")

            if not cleaned:
                if allow_empty:
                    return ""
                print(f"[Retry {attempt}/{MAX_RETRIES}] {tag} empty response; retrying.")
                time.sleep(min(30, 2 ** attempt))
                continue

            if validator is not None:
                ok, reason = validator(cleaned)
                if not ok:
                    preview = cleaned[:200].replace("\n", " ")
                    print(f"[Retry {attempt}/{MAX_RETRIES}] {tag} invalid ({reason}); "
                          f"retrying. preview: {preview!r}")
                    time.sleep(min(30, 2 ** attempt))
                    continue

            return cleaned

        except Exception as error:
            print(f"[Retry {attempt}/{MAX_RETRIES}] {tag} API error: {error}")
            time.sleep(min(60, 2 ** attempt))

    print(f"[Error] {tag} exhausted {MAX_RETRIES} retries; returning empty.")
    return ""


# ===============================
# Markdown table parsers + helpers
# ===============================

def _ParseMarkdownRows(text: str) -> List[List[str]]:
    rows: List[List[str]] = []
    if not text:
        return rows
    for raw in text.strip().split("\n"):
        line = raw.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.split("|") if c.strip() != ""]
        if cells:
            rows.append(cells)
    return rows


def IsValidMarkdownTable(text: str, min_data_rows: int = 1, min_cells: int = 4) -> Tuple[bool, str]:
    if not text or not text.strip():
        return False, "empty response"
    has_separator = any("---" in line and line.lstrip().startswith("|")
                        for line in text.splitlines())
    rows = _ParseMarkdownRows(text)
    qualifying = [r for r in rows if len(r) >= min_cells]
    if not has_separator:
        return False, "no markdown separator row"
    if len(qualifying) < min_data_rows + 1:
        return False, (f"only {len(qualifying)} rows with >={min_cells} cells "
                       f"(need header + {min_data_rows} data)")
    return True, ""


def ParseExtractionTable(md_text: str) -> List[Dict[str, str]]:
    """Parse a 4-column commitment table into
    [{commitment, year_x, year_y, page_x}, ...]."""
    rows: List[Dict[str, str]] = []
    if not md_text:
        return rows
    for raw in md_text.strip().split("\n"):
        line = raw.strip()
        if not line.startswith("|") or "---" in line:
            continue
        if "承諾來源頁碼" in line or "目標年份" in line:
            continue
        cells = [p.strip() for p in line.split("|") if p.strip() != ""]
        if len(cells) < 4:
            continue
        commitment = cells[0].replace("**", "").strip()
        if not commitment or commitment == "承諾" or "..." in commitment:
            continue
        rows.append({
            "commitment": commitment,
            "year_x": cells[1],
            "year_y": cells[2],
            "page_x": cells[3],
        })
    return rows


_CLUE_DELIM = re.compile(r"<br\s*/?>|\n", flags=re.IGNORECASE)
_CLUE_EMPTY = {"", "無", "無資料", "none", "None", "-", "—"}


def _SplitClues(cell: str, limit: int = None) -> List[str]:
    """Split a 相關線索 cell into individual clue strings on <br> (or newline),
    dropping empties / placeholders, capped at `limit` (default
    MAX_CLUES_PER_CHUNK)."""
    if not cell:
        return []
    cap = MAX_CLUES_PER_CHUNK if limit is None else limit
    out: List[str] = []
    for part in _CLUE_DELIM.split(cell):
        p = part.strip()
        if p in _CLUE_EMPTY:
            continue
        out.append(p)
        if len(out) >= cap:
            break
    return out


def ParseVerificationTable(md_text: str) -> Dict[str, Dict]:
    """Parse a verification table (>=4 columns) into
    {commitment -> {status, evidence, page_y, clues}}. The 5th column (相關線索)
    is optional and may hold up to MAX_CLUES_PER_CHUNK clues separated by <br>;
    legacy 4-column output parses with clues=[]."""
    out: Dict[str, Dict] = {}
    if not md_text:
        return out
    for raw in md_text.strip().split("\n"):
        line = raw.strip()
        if not line.startswith("|") or "---" in line:
            continue
        if "達成狀態" in line:
            continue
        cells = [p.strip() for p in line.split("|") if p.strip() != ""]
        if len(cells) < 4:
            continue
        commitment = cells[0].replace("**", "").strip()
        if not commitment or commitment == "承諾":
            continue
        clue_cell = " ".join(cells[4:]).strip() if len(cells) >= 5 else ""
        out[commitment] = {
            "status": cells[1],
            "evidence": cells[2],
            "page_y": cells[3],
            "clues": _SplitClues(clue_cell),
        }
    return out


def ParseCheckResultTable(md_text: str) -> List[Dict[str, str]]:
    """Parse an 8-column check_result.md table

    | 承諾 | 發布年份 | 目標年份 | 達成狀態 | 證據 | 承諾來源頁碼 | 證據來源頁碼 | 其他相關/可疑證據 |

    into [{commitment, year_x, year_y, status, evidence, page_x, page_y, clues}].
    The trailing clue cell is split back into a list. Rows are written with
    SanitizeCell / CluesToCell so every cell is pipe-safe and the column count is
    stable; we therefore require >= 7 cells (clue column optional for safety)."""
    rows: List[Dict[str, str]] = []
    if not md_text:
        return rows
    for raw in md_text.strip().split("\n"):
        line = raw.strip()
        if not line.startswith("|") or "---" in line:
            continue
        if "達成狀態" in line and "目標年份" in line:
            continue
        cells = [p.strip() for p in line.split("|") if p.strip() != ""]
        if len(cells) < 7:
            continue
        commitment = cells[0].replace("**", "").strip()
        if not commitment or commitment == "承諾":
            continue
        clue_cell = cells[7] if len(cells) >= 8 else ""
        rows.append({
            "commitment": commitment,
            "year_x": cells[1],
            "year_y": cells[2],
            "status": cells[3],
            "evidence": cells[4],
            "page_x": cells[5],
            "page_y": cells[6],
            "clues": _SplitClues(clue_cell, limit=MAX_CLUES_PER_COMMITMENT),
        })
    return rows


def CommitmentsToMarkdown(rows: List[Dict[str, str]]) -> str:
    lines = [
        "| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |",
        "|---|---|---|---|",
    ]
    for c in rows:
        lines.append(
            f"| {c['commitment']} | {c['year_x']} | {c['year_y']} | {c['page_x']} |"
        )
    return "\n".join(lines)


def CluesToCell(clues: List[str]) -> str:
    """Render a related-clue list into one safe Markdown table cell: strip
    newlines, escape pipes (-> '／') so they don't break the table, join with
    <br>. Empty list -> '無'."""
    if not clues:
        return "無"
    safe = []
    for clue in clues:
        cell = (clue or "").replace("\r", " ").replace("\n", " ")
        cell = cell.replace("|", "／").strip()
        if cell:
            safe.append(cell)
    return "<br>".join(safe) if safe else "無"


def SanitizeCell(text: str) -> str:
    """Make an arbitrary string safe inside a single Markdown table cell: collapse
    newlines and escape pipes (-> '／') so verbatim ESG 原文 摘錄 can never split a
    row into the wrong number of columns. Empty -> '無資料' (keeps the column
    count stable so the table stays parseable downstream)."""
    if text is None:
        return "無資料"
    cell = str(text).replace("\r", " ").replace("\n", " ").replace("|", "／").strip()
    return cell or "無資料"


_ROC_YEAR_RE = re.compile(r"\d{2,3}")


def _ParseRocYear(value: str) -> Optional[int]:
    """Largest 2-3 digit ROC year in a cell. Handles '民國114年' and ranges
    like '110~115' (returns the farthest/largest year)."""
    if value is None:
        return None
    nums = _ROC_YEAR_RE.findall(str(value))
    return max(int(n) for n in nums) if nums else None


def IsFutureTargetYear(year_y: str, year_x: str) -> bool:
    """True iff year_y parses to a ROC year strictly greater than year_x.
    Keeps only commitments whose target year is in the future (>= year_x + 1)."""
    y = _ParseRocYear(year_y)
    x = _ParseRocYear(year_x)
    if y is None or x is None:
        return False
    return y > x


def TargetsYearOrLater(year_y: str, year_floor: str) -> bool:
    """True iff year_y parses to a ROC year >= year_floor.

    Used when verifying year Y to collect every prior commitment whose target is
    Y OR any later year. Pulling in later-target commitments lets us catch ones
    fulfilled EARLY (their evidence shows up in Y's report even though the target
    year is still ahead), instead of only seeing them in the target year."""
    y = _ParseRocYear(year_y)
    f = _ParseRocYear(year_floor)
    if y is None or f is None:
        return False
    return y >= f


def SourceBeforeYear(year_x: str, year_ref: str) -> bool:
    """True iff year_x (發布年份) parses to a ROC year strictly less than year_ref.

    Used when verifying year Y to keep only commitments DECLARED in an EARLIER
    report (發布年份 < Y). A promise first stated in Y's own report has nothing to
    verify against that same report yet — its verification starts the next year.
    The canonical 發布年份 is the earliest declaring year, so this correctly admits
    a promise as soon as any prior report has stated it."""
    x = _ParseRocYear(year_x)
    r = _ParseRocYear(year_ref)
    if x is None or r is None:
        return False
    return x < r


def _MatchesCanonical(chunk_commitment: str, canonical_commitment: str) -> bool:
    a = (chunk_commitment or "").strip()
    b = (canonical_commitment or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return a in b or b in a


def AggregatePerCommitment(
    canonical_commitments: List[Dict[str, str]],
    all_chunk_results: List[Dict[str, str]],
) -> List[Dict]:
    """For each canonical commitment decide the final status by code:
        - any chunk 已達成 -> 已達成 (candidates = those 已達成 rows)
        - all chunks 未提及 / none matched -> 未提及 (no candidates)
        - else -> 部分達成 (candidates = 已達成 ∪ 部分達成 rows)

    Also gathers up to MAX_CLUES_PER_COMMITMENT deduped 相關線索 snippets across
    every matching chunk row (regardless of status) as a human-review aid; the
    result is stored on entry["clues"] (list[str], may be empty).
    """
    out: List[Dict] = []
    for c in canonical_commitments:
        canon_text = c["commitment"]
        related = [r for r in all_chunk_results
                   if _MatchesCanonical(r["commitment"], canon_text)]
        statuses = [r["status"] for r in related]

        if "已達成" in statuses:
            final_status = "已達成"
            candidates = [r for r in related if r["status"] == "已達成"]
        elif related and all(s == "未提及" for s in statuses):
            final_status = "未提及"
            candidates = []
        elif not related:
            final_status = "未提及"
            candidates = []
        else:
            final_status = "部分達成"
            candidates = [r for r in related if r["status"] in ("已達成", "部分達成")]

        clues: List[str] = []
        seen_clues = set()
        for r in related:
            for clue in r.get("clues", []):
                c_norm = (clue or "").strip()
                if not c_norm or c_norm in ("無", "無資料", "none", "None", "-"):
                    continue
                if c_norm in seen_clues:
                    continue
                seen_clues.add(c_norm)
                clues.append(c_norm)
            if len(clues) >= MAX_CLUES_PER_COMMITMENT:
                break
        del clues[MAX_CLUES_PER_COMMITMENT:]

        out.append({**c, "status": final_status,
                    "candidates": candidates, "clues": clues})
    return out


# ===============================
# Extraction: all future-year commitments from a PDF chunk
# ===============================

def _ExtractAllPromisesPrompt(start_page: int, end_page: int, year_x: str) -> str:
    try:
        min_year_y = int(year_x) + 1
        future_hint = f"必須 ≥ 民國 {min_year_y} 年"
        example_year = str(min_year_y)
    except (TypeError, ValueError):
        future_hint = f"必須大於發布年份 {year_x}"
        example_year = "民國年"
    return f"""你是一個專業的 ESG 報告分析師。
附件是一份 ESG 報告 PDF（這幾頁對應原報告第 {start_page}~{end_page} 頁，報告發布年份為民國 {year_x} 年）。
你的任務是「完整、不遺漏」地提取公司在這份附件中明確標示「未來具體目標年份」的承諾，並標註該目標民國年份。

【規則】
1. 必須列出附件中「所有」符合條件的承諾，逐項列出，不可遺漏、不可省略；同一頁有多個承諾就輸出多列。
2. 目標年份必須在「未來」：嚴格大於報告發布年份 {year_x}（{future_hint}）。凡目標年份等於或早於 {year_x}（已到期或當年度）的承諾，一律不列入。
3. 只擷取目標年份「明確、單一」且為民國年數字的承諾；「未來」、「長期」、「中長期」等沒有具體年份的不列入。
4. 若同一承諾原文是區間（如「{year_x}-{example_year}」、「{year_x}~{example_year}」），只取最遠的那個年份；若條列多個年份，請拆成多列分別輸出，且每列年份都須符合規則 2。
5. 排除得獎、排名、認證、評分等外部肯定，只列公司主動實施的具體行動、專案或目標。
6. 「承諾」欄位精簡核心內容，不要在敘述中寫年份。
7. 「發布年份」固定填 {year_x}。
8. 「目標年份」填具體民國年數字，且必須大於 {year_x}。
9. 「承諾來源頁碼」必須是 {start_page}~{end_page} 之間的單一頁碼數字（以報告印刷頁碼為準）。
10. 若附件中完全找不到符合條件的承諾，請完全不輸出任何文字（連表格框、標題都不要）。
11. 嚴禁輸出推理過程、<think> 標籤、解釋、```markdown 包覆；只輸出純表格。

---
**輸出格式**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | {year_x} | (民國年，> {year_x}) | (頁碼) |
---
"""


def _ExtractTableValidator(text: str) -> Tuple[bool, str]:
    if not text or not text.strip():
        return True, ""
    return IsValidMarkdownTable(text, min_data_rows=1, min_cells=4)


def ExtractPromisesFromChunk(
    client: OpenAI, model: str, pdf_bytes: bytes,
    start_page: int, end_page: int, year_x: str,
) -> str:
    prompt = _ExtractAllPromisesPrompt(start_page, end_page, year_x)
    content = _BuildPdfContent(prompt, pdf_bytes, f"pages_{start_page}_{end_page}.pdf")
    return CallOpenaiWithRetry(
        client, model, content,
        max_completion_tokens=8192,
        validator=_ExtractTableValidator,
        allow_empty=True,
        label=f"extract p{start_page}-{end_page}",
    )


def ExtractModelPromises(
    client: OpenAI, model_name: str, pdf_path: str, year_x: str,
) -> List[Dict[str, str]]:
    """Walk the report PDF in overlapping chunks and return the model's
    de-duplicated (by exact commitment text) list of commitments whose target
    year is strictly after year_x (future-only)."""
    print(f"\n[{model_name}] extracting all future-year promises from {pdf_path} "
          f"({PAGES_PER_CHUNK}-page chunks, {CHUNK_OVERLAP}-page overlap)")
    extracted_rows: List[Dict[str, str]] = []

    for start_page, end_page, pdf_bytes in IterPdfChunks(pdf_path):
        print(f"\n  [{model_name}] extract pages {start_page}-{end_page}")
        md_result = ExtractPromisesFromChunk(
            client, model_name, pdf_bytes, start_page, end_page, year_x
        )
        if md_result and md_result.strip():
            print(f"--- [{model_name}] Extracted ({start_page}-{end_page}) ---")
            print(md_result)
            new_rows = ParseExtractionTable(md_result)
            kept = [r for r in new_rows
                    if IsFutureTargetYear(r.get("year_y", ""), year_x)]
            dropped = len(new_rows) - len(kept)
            if dropped:
                print(f"  [{model_name}] dropped {dropped} row(s) whose 目標年份 "
                      f"is not in the future (> {year_x})")
            extracted_rows.extend(kept)

    unique = list({row["commitment"]: row
                   for row in extracted_rows if row["commitment"]}.values())
    return unique


# ===============================
# Verification: canonical list against a PDF chunk
# ===============================

def _VerifyPrompt(start_page: int, end_page: int, promises_md: str) -> str:
    return f"""你是一個嚴格的 ESG 檢核員。
附件是一份 ESG 報告 PDF（這幾頁對應原報告第 {start_page}~{end_page} 頁）。
你的任務是檢驗附件內容是否包含以下【前報告的承諾清單】的具體達成證據或相關線索。

【前報告的承諾清單】
{promises_md}

【只輸出「有證據或有線索」的承諾（最重要）】
- 「只」列出在這幾頁中，該承諾「找得到具體達成證據」或「找得到相關線索」的列。
- 凡是在這幾頁中完全沒有任何相關內容（既無達成證據、也無相關線索）的承諾，請「完全不要」為它輸出任何列；「不需要」列出「未提及」且毫無線索的承諾。
- 若這幾頁中所有承諾都沒有證據也沒有線索，請「完全不要輸出任何文字」（連表頭、表格框都不要）。
- 嚴禁新增清單上沒有的承諾，承諾文字須與清單一致。

【檢核規則】
1. 「達成狀態」只能填：「已達成」、「部分達成」、「未提及」三者之一。
   - 「已達成」／「部分達成」：必須在「證據」欄精確摘錄附件中的原文，不可改寫。
   - 「未提及」：僅用於「沒有具體達成證據、但有相關線索」而被列出的承諾；此時「證據」與「證據來源頁碼」一律填「無資料」，把相關片段放到「相關線索」欄。
2. 【最重要】「只是重申承諾」不能作為達成證據：若附件只是再次宣示同一個目標、重複承諾內容、表達決心、願景或口號，而沒有任何已實現的內容，一律「不可」判為已達成或部分達成。
   證據必須是「具體、完整的數據或實際事跡」，例如：實際達成的數值或百分比、已完成或進行中的專案、已導入的具體措施、第三方查證／驗證結果等可佐證「已經做了什麼」的內容。
   若附件對該承諾「只有目標宣示／重申，而無任何已實現的數據或事跡」，請「不要」判為已達成或部分達成；可改列為「未提及」並把該宣示句放到「相關線索」欄供人工複核。
3. 「證據來源頁碼」必須是 {start_page}~{end_page} 之間的單一頁碼數字（以報告印刷頁碼為準）。
4. 「相關線索」欄位（供人工複核參考，不影響達成狀態判定）：列出附件中與該承諾「主題或指標相關、但不足以單獨判定為已達成」的原文片段，**最多 3 條**。
   - 適用情境：提到同一主題／指標卻沒有具體進度或數字、只有口號宣示、數字方向或範圍不完全吻合、疑似但無法確定是否為同一件事。
   - 「即使本列『證據』欄已經填寫」，只要附件中還有其他相關或疑似片段，也請一併列在此欄，方便人工複核時有更多依據可調整或修正。
   - 每一條都必須是附件中「真實存在」的原文，嚴禁杜撰或改寫；每條後面標注頁碼，例如「…（第12頁）」。
   - 多條之間一律用 <br> 分隔（例如：片段一（第3頁）<br>片段二（第5頁）<br>片段三（第8頁））。
   - 不要把已填在「證據」欄的同一句話重複放進此欄。
   - 「已達成／部分達成」的列若沒有其他額外片段，此欄可填「無」；但若本列是以「未提及」被列出的，此欄「不可」為「無」（必須有讓你列出它的線索）。
5. 嚴禁輸出推理過程、<think> 標籤、解釋、```markdown 包覆；只輸出純表格（無任何符合項目時則輸出空白）。

---
**輸出格式**

| 承諾 | 達成狀態 | 證據 | 證據來源頁碼 | 相關線索 |
|---|---|---|---|---|
| (有證據或線索的承諾) | (已達成/部分達成/未提及) | (摘錄原文/無資料) | (頁碼/無資料) | (最多3條相關原文＋頁碼，以 <br> 分隔／無) |
---
"""


def _VerifyTableValidator(text: str) -> Tuple[bool, str]:
    """Lenient verify validator. The verify pass now emits ONLY the commitments a
    chunk actually evidences or has clues for, so a clean empty response (no
    matches in this chunk) is legitimate and accepted. A non-empty response must
    still be a well-formed >=4-column Markdown table."""
    if not text or not text.strip():
        return True, ""
    return IsValidMarkdownTable(text, min_data_rows=1, min_cells=4)


def VerifyChunk(
    client: OpenAI, model: str, pdf_bytes: bytes,
    start_page: int, end_page: int, promises_md: str,
) -> str:
    prompt = _VerifyPrompt(start_page, end_page, promises_md)
    content = _BuildPdfContent(prompt, pdf_bytes, f"pages_{start_page}_{end_page}.pdf")
    return CallOpenaiWithRetry(
        client, model, content,
        max_completion_tokens=VERIFY_MAX_TOKENS,
        validator=_VerifyTableValidator,
        allow_empty=True,
        label=f"verify p{start_page}-{end_page}",
    )


def VerifyChunksForModel(
    client: OpenAI, model_name: str, pdf_path: str,
    canonical_promises_md: str,
) -> List[Dict[str, str]]:
    """Verify the canonical commitment list against the report PDF chunk by
    chunk. Each chunk emits only the commitments it evidences or has clues for;
    commitments no chunk touches default to 未提及 in AggregatePerCommitment.
    Returns every raw per-chunk per-commitment row (aggregation happens in
    AggregatePerCommitment)."""
    chunk_results: List[Dict[str, str]] = []
    for start_page, end_page, pdf_bytes in IterPdfChunks(pdf_path):
        print(f"  [{model_name}] verify pages {start_page}-{end_page}")
        verify_md = VerifyChunk(
            client, model_name, pdf_bytes, start_page, end_page,
            canonical_promises_md,
        )
        if not (verify_md and verify_md.strip()):
            continue

        print(f"--- [{model_name}] Verification ({start_page}-{end_page}) ---")
        print(verify_md)
        parsed = ParseVerificationTable(verify_md)
        chunk_pages = f"{start_page}-{end_page}"
        for comm_text, data in parsed.items():
            chunk_results.append({
                "model": model_name,
                "commitment": comm_text,
                "status": data["status"],
                "evidence": data["evidence"],
                "page_y": data["page_y"],
                "clues": data.get("clues", []),
                "chunk_pages": chunk_pages,
            })
    return chunk_results


# ===============================
# Judge: consolidate (dedup) commitment lists
# ===============================

def DedupAllPromisesWithText(
    client: OpenAI, per_model_tables_md: Dict[str, str], year_x: str,
) -> str:
    """Consolidate per-model commitment tables from the SAME year_x report into
    one canonical table: merge semantic duplicates, keep every distinct
    commitment, and keep only future target years (> year_x)."""
    sections = []
    for model_name, table_md in per_model_tables_md.items():
        body = (table_md or "").strip()
        if body:
            sections.append(f"### Model: {model_name}\n{body}")
    sources = "\n\n".join(sections) if sections else "(空)"

    # Pure-string fast path: with <=1 row total there is nothing to merge
    # semantically, so skip the judge LLM and return the rows directly.
    combined: List[Dict[str, str]] = []
    for table_md in per_model_tables_md.values():
        combined.extend(ParseExtractionTable(table_md or ""))
    if len(combined) <= 1:
        return CommitmentsToMarkdown(combined)

    try:
        future_hint = f"至少民國 {int(year_x) + 1} 年"
    except (TypeError, ValueError):
        future_hint = f"大於 {year_x}"

    prompt = f"""你是資深的 ESG 資料整理員。下面是 AI 模型從同一份民國 {year_x} 年 ESG 報告擷取出的承諾清單（每筆都帶有明確的目標民國年份）。

【任務】
1. 語意去重：合併描述同一件事的承諾。但若目標年份不同，即使措辭相似仍視為不同項目，絕不可合併。
2. 完整保留：除了「真正語意重複」的項目外，不可刪除或遺漏任何承諾，務必把所有承諾都列出。
3. 過濾雜訊：移除得獎、排名、認證、評分等外部肯定，或明顯非公司主動實施的項目。
4. 目標年份必須在未來：只保留目標年份「嚴格大於發布年份 {year_x}」（{future_hint}）的承諾；目標年份 ≤ {year_x} 的一律移除。
5. 統一描述：合併後挑最精簡、最清楚的措辭，不要在承諾文字裡寫年份。
6. 「發布年份」固定填 {year_x}。
7. 「目標年份」保留原始的民國年數字（且須 > {year_x}）。
8. 「承諾來源頁碼」合併時保留最小（最早）的頁碼；若全部為「無資料」則填「無資料」。
9. 嚴禁輸出推理過程、<think>、前言、結語、```markdown 包覆；只輸出最終表格。

【擷取的承諾清單】
{sources}

---
**輸出格式**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | {year_x} | (民國年，> {year_x}) | ... |
---
"""
    return CallOpenaiWithRetry(
        client, JUDGE_MODEL, prompt,
        max_completion_tokens=8192,
        validator=lambda t: IsValidMarkdownTable(t, 1, 4),
        allow_empty=False,
        label="judge-dedup-all",
    )


def DedupCrossYearPromisesWithText(
    client: OpenAI, per_source_tables_md: Dict[str, str], year_floor: str,
) -> str:
    """Consolidate commitment tables collected from one or more prior-year
    promise.md files. Unlike the single-report extraction dedup, these rows carry
    DIFFERENT target years (all >= year_floor, since a year may have been hit
    early): merge only semantic duplicates that ALSO share the same 目標年份,
    preserve every distinct target year verbatim, and report the earliest 發布年份
    + page per commitment."""
    sections = []
    for src, table_md in per_source_tables_md.items():
        body = (table_md or "").strip()
        if body:
            sections.append(f"### 來源報告：民國 {src} 年\n{body}")
    sources = "\n\n".join(sections) if sections else "(空)"

    # Pure-string fast path: with <=1 row total there is nothing to merge
    # semantically, so skip the judge LLM and return the row(s) directly.
    combined: List[Dict[str, str]] = []
    for table_md in per_source_tables_md.values():
        combined.extend(ParseExtractionTable(table_md or ""))
    if len(combined) <= 1:
        return CommitmentsToMarkdown(combined)

    prompt = f"""你是資深的 ESG 資料整理員。下方是公司一份或多份不同發布年份的 ESG 報告各自提到的承諾清單。這些承諾的「目標年份」不一定相同（皆為民國 {year_floor} 年或更晚），請整理出唯一的承諾清單。

【任務】
1. 語意去重：合併描述同一件事的承諾（例如「減 10% 碳排」與「降低碳排放 10%」視為同一項）。但若「目標年份不同」，即使措辭相似仍視為「不同項目」，絕不可合併。
2. 完整保留：除了「目標年份相同且語意重複」的項目外，不可刪除或遺漏任何承諾，務必把所有承諾、所有不同的目標年份都各自列出。
3. 統一描述：合併後挑最精簡、最清楚的措辭，不要在承諾文字裡寫年份。
4. 「發布年份」保留所有提及該承諾的來源報告中「最早」的民國年份。
5. 「目標年份」保留承諾原本的民國年數字，不可改寫、不可統一成同一年。
6. 「承諾來源頁碼」保留「最早來源報告」的頁碼；若該來源報告對該承諾多列有不同頁碼，取最小者。
7. 嚴禁推理、<think>、前言、結語、```markdown 包覆；只輸出純表格。

【各年度報告擷取的承諾清單】
{sources}

---
**輸出格式**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | (最早出現的民國年份) | (該承諾原本的目標民國年份) | ... |
---
"""
    return CallOpenaiWithRetry(
        client, JUDGE_MODEL, prompt,
        max_completion_tokens=8192,
        validator=lambda t: IsValidMarkdownTable(t, 1, 4),
        allow_empty=False,
        label="judge-dedup-crossyear",
    )


def DedupSingleTargetPromisesWithText(
    client: OpenAI, per_source_tables_md: Dict[str, str], target_year: str,
) -> str:
    """Consolidate commitment tables that ALL target the SAME year (target_year),
    gathered from one or more declaring-year promise.md files, into one canonical
    list: merge semantic duplicates, keep every distinct commitment, fix 目標年份
    to target_year, and report the earliest 發布年份 + page per commitment.

    This is run once per target year (by openai_consolidate_promises.py) so that
    a given future promise gets ONE fixed wording, shared by every year that later
    verifies it."""
    sections = []
    for src, table_md in per_source_tables_md.items():
        body = (table_md or "").strip()
        if body:
            sections.append(f"### 來源報告：民國 {src} 年\n{body}")
    sources = "\n\n".join(sections) if sections else "(空)"

    # Pure-string fast path: with <=1 row total there is nothing to merge.
    combined: List[Dict[str, str]] = []
    for table_md in per_source_tables_md.values():
        combined.extend(ParseExtractionTable(table_md or ""))
    if len(combined) <= 1:
        return CommitmentsToMarkdown(combined)

    prompt = f"""你是資深的 ESG 資料整理員。下方是公司一份或多份不同發布年份的 ESG 報告各自提到的「目標年份為民國 {target_year} 年」的承諾清單。請整理出唯一的承諾清單。

【任務】
1. 語意去重：合併描述同一件事的承諾（例如「減 10% 碳排」與「降低碳排放 10%」視為同一項）。除了「真正語意重複／相同」的項目外，不可刪除或遺漏任何承諾，務必把所有承諾都列出。
2. 統一描述：合併後挑最精簡、最清楚的措辭作為「唯一的標準敘述」，不要在承諾文字裡寫年份。請以後續每一年都會沿用這段文字為前提，用語務求清楚、穩定、可重複使用。
3. 「發布年份」保留所有提及該承諾的來源報告中「最早」的民國年份。
4. 「目標年份」固定填 {target_year}。
5. 「承諾來源頁碼」保留「最早來源報告」的頁碼；若該來源報告對該承諾多列有不同頁碼，取最小者。
6. 嚴禁推理、<think>、前言、結語、```markdown 包覆；只輸出純表格。

【各年度報告擷取的承諾清單】
{sources}

---
**輸出格式**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | (最早出現的民國年份) | {target_year} | ... |
---
"""
    return CallOpenaiWithRetry(
        client, JUDGE_MODEL, prompt,
        max_completion_tokens=8192,
        validator=lambda t: IsValidMarkdownTable(t, 1, 4),
        allow_empty=False,
        label=f"judge-dedup-target-{target_year}",
    )


def LoadCanonicalPromisesForYear(result_dir: str, company: str, year: str) -> str:
    """Concatenate the pre-consolidated promise/{T}.md canonical lists for every
    target year T >= year into ONE 4-col table.

    These files are produced by openai_consolidate_promises.py and guarantee
    identical wording for a given promise across every year that tracks it (so a
    commitment fulfilled early reads the same in year Z as in its target year Y).
    Returns "" when the promise/ dir is absent or yields no rows, so the caller
    can fall back to per-run collection + consolidation."""
    pdir = os.path.join(result_dir, company, "promise")
    if not os.path.isdir(pdir):
        return ""
    year_int = _ParseRocYear(year)
    rows: List[Dict[str, str]] = []
    for name in sorted(os.listdir(pdir)):
        if not name.lower().endswith(".md"):
            continue
        t = _ParseRocYear(name[:-3])
        if t is None:
            continue
        if year_int is not None and t < year_int:
            continue
        with open(os.path.join(pdir, name), "r", encoding="utf-8") as f:
            rows.extend(ParseExtractionTable(f.read()))
    return CommitmentsToMarkdown(rows) if rows else ""


# ===============================
# Judge: per-commitment best-evidence picker
# ===============================

_EVIDENCE_LINE = re.compile(r"^\s*(?:\*\*)?證據(?:原文)?(?:\*\*)?\s*[:：]\s*(.*?)\s*(?:\*\*)?\s*$")
_PAGE_LINE = re.compile(r"^\s*(?:\*\*)?(?:證據來源)?頁碼(?:\*\*)?\s*[:：]\s*(.*?)\s*(?:\*\*)?\s*$")


def PickBestEvidenceWithText(
    client: OpenAI, commitment: str, year_y: str, candidates: List[Dict[str, str]],
) -> Tuple[str, str]:
    """Pick the single best evidence for one commitment from the per-chunk
    candidate list. Returns (evidence_text, page_y); ('無資料', '無資料') when
    candidates is empty (no model call)."""
    if not candidates:
        return "無資料", "無資料"

    # Pure-string fast path: with a single candidate there is no semantic choice
    # to make, so take it directly instead of spending an LLM call.
    if len(candidates) == 1:
        only = candidates[0]
        evidence = (only.get("evidence") or "").strip() or "無資料"
        page = (only.get("page_y") or "").strip() or "無資料"
        return evidence, page

    lines = []
    for i, c in enumerate(candidates, 1):
        evidence = (c.get("evidence") or "").strip() or "無資料"
        page = (c.get("page_y") or "").strip() or "無資料"
        model = c.get("model", "?")
        status = c.get("status", "?")
        lines.append(f"{i}. (頁 {page} / 模型 {model} / 判定 {status})\n   {evidence}")
    candidates_block = "\n".join(lines)

    prompt = f"""你是嚴格的 ESG 檢核員。下方是針對「同一條承諾」在民國 {year_y} 年 ESG 報告各分頁中找到的候選證據。請從中挑出單一「最能直接、精確佐證該承諾」的證據。

【承諾】
{commitment}

【候選證據】
{candidates_block}

【挑選規則】
1. 必須從上方候選裡選一筆，禁止自行撰寫或改寫文字；原文照抄。
2. 「頁碼」必須來自所選候選對應的頁碼數字，不可使用其他候選的頁碼。
3. 「只是重申承諾」不算證據：僅再次宣示目標、重複承諾內容、表達決心或願景、口號等，皆不可當作達成證據。請優先挑選「有具體、完整數據或實際事跡」（實際數值／百分比、已完成或進行中的專案、已導入的措施、第三方查證結果等）的候選。
4. 若候選都僅是目標宣示／重申承諾、無資料、與承諾無關，或全部為「無資料」，請輸出 證據: 無資料 / 頁碼: 無資料。
5. 嚴禁推理、<think>、前言、結語、解釋、```markdown 包覆。

【輸出格式】（只輸出兩行）
證據: <原文摘錄或「無資料」>
頁碼: <對應頁碼或「無資料」>
"""

    def _pick_validator(text: str) -> Tuple[bool, str]:
        if not text or not text.strip():
            return False, "empty response"
        has_ev = any(_EVIDENCE_LINE.match(l) for l in text.splitlines())
        has_pg = any(_PAGE_LINE.match(l) for l in text.splitlines())
        if not (has_ev and has_pg):
            return False, "missing 證據/頁碼 line"
        return True, ""

    raw = CallOpenaiWithRetry(
        client, JUDGE_MODEL, prompt,
        max_completion_tokens=512,
        validator=_pick_validator,
        allow_empty=False,
        label="judge-pick-evidence",
    )

    evidence = "無資料"
    page = "無資料"
    for line in raw.splitlines():
        ev_match = _EVIDENCE_LINE.match(line)
        if ev_match:
            evidence = ev_match.group(1).strip() or "無資料"
            continue
        pg_match = _PAGE_LINE.match(line)
        if pg_match:
            page = pg_match.group(1).strip() or "無資料"
    return evidence, page


# ===============================
# Cross-year clustering (pure code, no LLM / no PDF reads)
#
# A commitment targeting year Y may be verified across several reports (an early
# fulfilment shows 已達成 in the year it was actually delivered, not in Y).
# _ClusterTargetYearRows groups the per-year check_result.md rows for one target
# year into per-commitment clusters; the timeline builder below turns each cluster
# into one structured promise record. Reads only the markdown already on disk.
# ===============================


def _ClusterTargetYearRows(
    per_year: Dict[int, List[Dict[str, str]]], year_int: int,
) -> List[Dict]:
    """Cluster every check_result.md row whose 目標年份 == year_int (across the
    report years Z <= year_int in `per_year`) into per-commitment groups.

    Returns [{rep, members:[(Z, row), ...]}, ...]. Rows are matched by semantic
    text alone (_MatchesCanonical): two rows targeting the same year with matching
    承諾 內容 are the same commitment even if a year assigned a different 發布年份
    (reconciled downstream). The longest matching text becomes the cluster rep."""
    tagged: List[Tuple[int, Dict[str, str]]] = []
    for z in sorted(per_year):
        if z > year_int:
            continue
        for row in per_year[z]:
            if _ParseRocYear(row.get("year_y", "")) == year_int:
                tagged.append((z, row))

    clusters: List[Dict] = []
    for z, row in tagged:
        text = row["commitment"]
        for cl in clusters:
            if _MatchesCanonical(cl["rep"], text):
                cl["members"].append((z, row))
                if len(text) > len(cl["rep"]):
                    cl["rep"] = text
                break
        else:
            clusters.append({"rep": text, "members": [(z, row)]})
    return clusters


# ===============================
# Per-year promise timelines (pure code)
#
# summary.json wants, for each promise, the verdict for EVERY report year that
# tracked it (declaration year + 1 .. target year). This section uses the
# cross-year clustering above and emits one structured record per commitment whose
# `timeline` lists each year's status / evidence / page / clues. Consumed by
# openai_build_summary_json.py. Reads only the per-year check_result.md files.
# ===============================

_STATUS_RANK = {"已達成": 2, "部分達成": 1, "未提及": 0}


def _PickBestStatusRow(rows: List[Dict[str, str]]) -> Dict[str, str]:
    """Highest-status row (已達成 > 部分達成 > 未提及) among same-year duplicates."""
    return max(rows, key=lambda r: _STATUS_RANK.get((r.get("status") or "").strip(), -1))


def _CollectAllCheckResults(result_dir: str, company: str) -> Dict[int, List[Dict[str, str]]]:
    """{Z: rows} for every numeric year dir Z holding a check_result.md (parsed
    via ParseCheckResultTable). Read once, reused across all target years."""
    base = os.path.join(result_dir, company)
    out: Dict[int, List[Dict[str, str]]] = {}
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        try:
            z = int(entry)
        except ValueError:
            continue
        path = os.path.join(base, entry, "check_result.md")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            out[z] = ParseCheckResultTable(f.read())
    return out


def _ClusterToTimeline(members: List[Tuple[int, Dict[str, str]]],
                       rep: str, year_int: int) -> Dict:
    """Turn one commitment's per-year check rows into a structured timeline dict:

        {commitment, declared_year, target_year, source_page,
         timeline: [{year, status, evidence, evidence_page, clues}, ...]}

    One timeline entry per report year (same-year duplicates folded to the best
    status, their clues unioned). evidence/evidence_page are null unless the
    year's verdict is 已達成/部分達成 with real (non-placeholder) text."""
    by_year: Dict[int, List[Dict[str, str]]] = {}
    for z, r in members:
        by_year.setdefault(z, []).append(r)

    declared_vals = [_ParseRocYear(r.get("year_x", "")) for _, r in members]
    declared_vals = [v for v in declared_vals if v is not None]
    declared_year = min(declared_vals) if declared_vals else None

    source_page = None
    for z in sorted(by_year):
        for r in by_year[z]:
            px = (r.get("page_x") or "").strip()
            if px and px not in _CLUE_EMPTY:
                source_page = px
                break
        if source_page:
            break

    timeline: List[Dict] = []
    for z in sorted(by_year):
        rows_z = by_year[z]
        best = _PickBestStatusRow(rows_z)
        status = (best.get("status") or "").strip()
        evidence = (best.get("evidence") or "").strip()
        has_ev = status in ("已達成", "部分達成") and evidence not in _CLUE_EMPTY
        page_y = (best.get("page_y") or "").strip()

        clues: List[str] = []
        seen = set()
        for r in rows_z:
            for c in r.get("clues", []):
                cc = (c or "").strip()
                if cc and cc not in _CLUE_EMPTY and cc not in seen:
                    seen.add(cc)
                    clues.append(cc)

        timeline.append({
            "year": z,
            "status": status,
            "evidence": evidence if has_ev else None,
            "evidence_page": page_y if (has_ev and page_y not in _CLUE_EMPTY) else None,
            "clues": clues,
        })

    return {
        "commitment": rep,
        "declared_year": declared_year,
        "target_year": year_int,
        "source_page": source_page,
        "timeline": timeline,
    }


def BuildCompanyPromiseTimelines(result_dir: str, company: str) -> List[Dict]:
    """Every commitment for a company, each with its per-year verification
    timeline folded from the per-year check_result.md files.

    For each distinct 目標年份 Y found in the check_result.md tables, the rows
    targeting Y are clustered across years (via _ClusterTargetYearRows) and each
    cluster becomes one promise dict (see _ClusterToTimeline).
    `timeline` therefore holds one verdict per report year that actually checked
    the promise (declaration year + 1 .. Y, for the report years that exist).

    Pure code: reads only check_result.md (no LLM, no PDF)."""
    per_year = _CollectAllCheckResults(result_dir, company)
    if not per_year:
        return []

    target_years = set()
    for rows in per_year.values():
        for row in rows:
            t = _ParseRocYear(row.get("year_y", ""))
            if t is not None:
                target_years.add(t)

    out: List[Dict] = []
    for y in sorted(target_years):
        for cl in _ClusterTargetYearRows(per_year, y):
            out.append(_ClusterToTimeline(cl["members"], cl["rep"], y))
    return out
