"""Shared utilities for the OpenAI PDF-file ESG promise-verification pipeline.

English-prompt edition: the six prompt builders below instruct the model in
English, but they demand the SAME Traditional-Chinese output schema as the
original Chinese edition (../chinese/openai_utils.py) — table headers, status
labels and placeholders stay Chinese because the parsers in this module key off
those exact tokens. Apart from the prompt builders and a few English log
messages, the code is identical to the Chinese edition, so both editions produce
interchangeable results.

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
        future_hint = f"must be >= ROC year {min_year_y}"
        example_year = str(min_year_y)
    except (TypeError, ValueError):
        future_hint = f"must be greater than the declared year {year_x}"
        example_year = "ROC year"
    return f"""You are a professional ESG report analyst.
The attachment is an ESG report PDF (these pages correspond to pages {start_page}~{end_page} of the original report; the report's publication year is ROC year {year_x}).
Your task is to extract, completely and without omission, every commitment in this attachment that explicitly states a specific FUTURE target year, and to label that target ROC year.

[Rules]
1. List ALL qualifying commitments in the attachment, item by item — never omit or abbreviate. If one page has several commitments, output several rows.
2. The target year must be in the FUTURE: strictly greater than the publication year {year_x} ({future_hint}). Any commitment whose target year equals or precedes {year_x} (already due, or current-year) must be excluded.
3. Only extract commitments whose target year is explicit, single and a ROC-year number; vague ones such as 「未來」 (future), 「長期」 (long-term), 「中長期」 (mid-to-long term) with no concrete year are excluded.
4. If a commitment's original text gives a range (e.g. 「{year_x}-{example_year}」, 「{year_x}~{example_year}」), take only the farthest year; if it lists several years, split them into several rows, and every row's year must satisfy Rule 2.
5. Exclude awards, rankings, certifications, ratings and other external recognition; list only concrete actions, projects or targets the company itself undertakes.
6. Keep the 承諾 (commitment) cell to the concise core statement, with no year inside the text. Keep the commitment text in Traditional Chinese exactly as worded in the report — do NOT translate it into English.
7. The 發布年份 (declared year) cell is always {year_x}.
8. The 目標年份 (target year) cell is a concrete ROC-year number and must be > {year_x}. If the report states a Gregorian year, convert it to the ROC year (ROC = Gregorian - 1911; e.g. 2030 -> 119) and output the ROC number only.
9. The 承諾來源頁碼 (source page) cell must be a single page number between {start_page} and {end_page} (using the report's printed page numbers).
10. If no qualifying commitment exists in the attachment, output nothing at all (no table frame, no header, no text).
11. Never output reasoning, <think> tags, explanations or ```markdown wrapping; output the pure table only, using EXACTLY the Chinese headers below.

---
**Output format**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | {year_x} | (ROC year, > {year_x}) | (page) |
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
                print(f"  [{model_name}] dropped {dropped} row(s) whose target "
                      f"year is not in the future (> {year_x})")
            extracted_rows.extend(kept)

    unique = list({row["commitment"]: row
                   for row in extracted_rows if row["commitment"]}.values())
    return unique


# ===============================
# Verification: canonical list against a PDF chunk
# ===============================

def _VerifyPrompt(start_page: int, end_page: int, promises_md: str) -> str:
    return f"""You are a strict ESG auditor.
The attachment is an ESG report PDF (these pages correspond to pages {start_page}~{end_page} of the original report).
Your task is to check whether the attachment contains concrete achievement evidence, or related clues, for the commitments in the [prior-report commitment list] below.

[Prior-report commitment list]
{promises_md}

[Output ONLY commitments that have evidence or clues — MOST IMPORTANT]
- Output ONLY the rows where, within these pages, the commitment has concrete achievement evidence or a related clue.
- For any commitment with no related content at all in these pages (neither evidence nor a clue), output NO row for it; there is no need to list 未提及 (not-mentioned) commitments that have no clue.
- If none of the commitments has evidence or clues in these pages, output nothing at all (no header, no table frame).
- Never add commitments that are not on the list; copy the 承諾 (commitment) text character for character from the list — do not rephrase, translate or abbreviate it.

[Audit rules]
1. The 達成狀態 (status) cell may only be one of exactly these three Chinese labels: 「已達成」 (achieved), 「部分達成」 (partially achieved), 「未提及」 (not mentioned).
   - 已達成 / 部分達成: quote the attachment's original (Chinese) text EXACTLY in the 證據 (evidence) cell; no paraphrasing, no translating.
   - 未提及: only for a commitment listed because it has no concrete achievement evidence but does have a related clue; in that case fill both 證據 and 證據來源頁碼 with 「無資料」, and put the related snippet in the 相關線索 (related clues) cell.
2. [MOST IMPORTANT] Merely RESTATING the commitment is NOT achievement evidence: if the attachment only re-declares the same target, repeats the commitment, or expresses determination / vision / slogans with nothing actually delivered, it must NOT be judged 已達成 or 部分達成.
   Evidence must be concrete, complete data or actual accomplishments — e.g. actually-achieved figures or percentages, completed or in-progress projects, measures already implemented, third-party assurance/verification results: content that substantiates what has actually been DONE.
   If the attachment has only a target declaration / restatement for a commitment, with no delivered data or facts, do NOT judge it 已達成 or 部分達成; you may instead list it as 未提及 and put that declaration sentence into the 相關線索 cell for human review.
3. The 證據來源頁碼 (evidence page) cell must be a single page number between {start_page} and {end_page} (using the report's printed page numbers).
4. The 相關線索 (related clues) cell (a human-review aid; it does not affect the status verdict): list original snippets from the attachment that relate to the commitment's topic or metric but are not enough on their own to conclude achievement — AT MOST 3.
   - When to use: the same topic/metric is mentioned without concrete progress or numbers; slogans only; the number's direction or range does not fully match; suspected but not certain to be the same thing.
   - Even if this row's 證據 cell is already filled, if the attachment has other related or suspicious snippets, list them here too, so a human reviewer has more to work with.
   - Every item must be text that TRULY EXISTS in the attachment, quoted in the original Chinese; never fabricate or rewrite. Append the page after each item in the form 「（第N頁）」, e.g. 「…（第12頁）」.
   - Separate multiple items with <br> (e.g. snippet one（第3頁）<br>snippet two（第5頁）<br>snippet three（第8頁）).
   - Do not repeat a sentence already placed in the 證據 cell.
   - An 已達成 / 部分達成 row with no extra snippet may fill 「無」 here; but a row listed as 未提及 must NOT be 「無」 (it must contain the clue that made you list it).
5. Never output reasoning, <think> tags, explanations or ```markdown wrapping; output the pure table only (or blank when nothing matches), using EXACTLY the Chinese headers below.

---
**Output format**

| 承諾 | 達成狀態 | 證據 | 證據來源頁碼 | 相關線索 |
|---|---|---|---|---|
| (commitment with evidence or clue) | (已達成/部分達成/未提及) | (quoted original text / 無資料) | (page / 無資料) | (up to 3 related snippets + page, joined by <br> / 無) |
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
    sources = "\n\n".join(sections) if sections else "(empty)"

    # Pure-string fast path: with <=1 row total there is nothing to merge
    # semantically, so skip the judge LLM and return the rows directly.
    combined: List[Dict[str, str]] = []
    for table_md in per_model_tables_md.values():
        combined.extend(ParseExtractionTable(table_md or ""))
    if len(combined) <= 1:
        return CommitmentsToMarkdown(combined)

    try:
        future_hint = f"at least ROC year {int(year_x) + 1}"
    except (TypeError, ValueError):
        future_hint = f"greater than {year_x}"

    prompt = f"""You are a senior ESG data curator. Below are commitment lists that AI models extracted from the SAME ROC year {year_x} ESG report (every row carries an explicit target ROC year).

[Task]
1. Semantic dedup: merge commitments that describe the same thing. But if the target years differ, treat them as different items even when the wording is similar — never merge them.
2. Keep everything: apart from truly semantically-duplicate items, never delete or omit any commitment; make sure every commitment is listed.
3. Filter noise: remove awards, rankings, certifications, ratings and other external recognition, or items clearly not undertaken by the company itself.
4. The target year must be in the future: keep only commitments whose target year is strictly greater than the declared year {year_x} ({future_hint}); remove any whose target year is <= {year_x}.
5. Unify the description: after merging, pick the most concise, clearest wording, kept in the original Traditional Chinese (never translate); do not write the year inside the commitment text.
6. The 發布年份 (declared year) cell is always {year_x}.
7. The 目標年份 (target year) cell keeps the original ROC-year number (and must be > {year_x}).
8. The 承諾來源頁碼 (source page) cell keeps the smallest (earliest) page number when merging; if all of them are 無資料, fill 無資料.
9. Never output reasoning, <think>, preamble, closing remarks or ```markdown wrapping; output the final table only, using EXACTLY the Chinese headers below.

[Extracted commitment lists]
{sources}

---
**Output format**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | {year_x} | (ROC year, > {year_x}) | ... |
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
            sections.append(f"### Source report: ROC year {src}\n{body}")
    sources = "\n\n".join(sections) if sections else "(empty)"

    # Pure-string fast path: with <=1 row total there is nothing to merge
    # semantically, so skip the judge LLM and return the row(s) directly.
    combined: List[Dict[str, str]] = []
    for table_md in per_source_tables_md.values():
        combined.extend(ParseExtractionTable(table_md or ""))
    if len(combined) <= 1:
        return CommitmentsToMarkdown(combined)

    prompt = f"""You are a senior ESG data curator. Below are the commitment lists mentioned by one or more of the company's ESG reports of different publication years. These commitments' target years are not necessarily the same (all of them are ROC year {year_floor} or later). Produce ONE unique commitment list.

[Task]
1. Semantic dedup: merge commitments that describe the same thing (e.g. 「減 10% 碳排」 and 「降低碳排放 10%」 are the same item). But if the target years differ, treat them as DIFFERENT items even when the wording is similar — never merge them.
2. Keep everything: apart from items that share the SAME target year AND are semantic duplicates, never delete or omit any commitment; list every commitment and every distinct target year separately.
3. Unify the description: after merging, pick the most concise, clearest wording, kept in the original Traditional Chinese (never translate); do not write the year inside the commitment text.
4. The 發布年份 (declared year) cell keeps the EARLIEST ROC year among all source reports that mention the commitment.
5. The 目標年份 (target year) cell keeps the commitment's original ROC-year number; never rewrite it or unify the years.
6. The 承諾來源頁碼 (source page) cell keeps the earliest source report's page; if that report lists the commitment with several different pages, take the smallest.
7. Never output reasoning, <think>, preamble, closing remarks or ```markdown wrapping; output the pure table only, using EXACTLY the Chinese headers below.

[Commitment lists extracted from each year's report]
{sources}

---
**Output format**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | (earliest declaring ROC year) | (the commitment's original target ROC year) | ... |
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
            sections.append(f"### Source report: ROC year {src}\n{body}")
    sources = "\n\n".join(sections) if sections else "(empty)"

    # Pure-string fast path: with <=1 row total there is nothing to merge.
    combined: List[Dict[str, str]] = []
    for table_md in per_source_tables_md.values():
        combined.extend(ParseExtractionTable(table_md or ""))
    if len(combined) <= 1:
        return CommitmentsToMarkdown(combined)

    prompt = f"""You are a senior ESG data curator. Below are the commitment lists — all with target year = ROC year {target_year} — mentioned by one or more of the company's ESG reports of different publication years. Produce ONE unique commitment list.

[Task]
1. Semantic dedup: merge commitments that describe the same thing (e.g. 「減 10% 碳排」 and 「降低碳排放 10%」 are the same item). Apart from truly semantically-duplicate/identical items, never delete or omit any commitment; make sure every commitment is listed.
2. Unify the description: after merging, pick the most concise, clearest wording as the SINGLE standard description, kept in the original Traditional Chinese (never translate); do not write the year inside the text. Assume every later year will reuse this exact wording, so make it clear, stable and reusable.
3. The 發布年份 (declared year) cell keeps the EARLIEST ROC year among all source reports that mention the commitment.
4. The 目標年份 (target year) cell is always {target_year}.
5. The 承諾來源頁碼 (source page) cell keeps the earliest source report's page; if that report lists the commitment with several different pages, take the smallest.
6. Never output reasoning, <think>, preamble, closing remarks or ```markdown wrapping; output the pure table only, using EXACTLY the Chinese headers below.

[Commitment lists extracted from each year's report]
{sources}

---
**Output format**

| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | (earliest declaring ROC year) | {target_year} | ... |
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
        lines.append(f"{i}. (page {page} / model {model} / status {status})\n   {evidence}")
    candidates_block = "\n".join(lines)

    prompt = f"""You are a strict ESG auditor. Below are the candidate pieces of evidence found across the page chunks of the ROC year {year_y} ESG report for THE SAME commitment. Pick the single candidate that most directly and precisely substantiates the commitment.

[Commitment]
{commitment}

[Candidate evidence]
{candidates_block}

[Selection rules]
1. You must pick one of the candidates above; never write or rewrite text yourself — copy the chosen candidate's original text verbatim.
2. The page number must come from the chosen candidate; never use another candidate's page.
3. Merely RESTATING the commitment is not evidence: re-declaring the target, repeating the commitment, or expressing determination / vision / slogans does not count as achievement evidence. Prefer candidates with concrete, complete data or actual accomplishments (real figures/percentages, completed or in-progress projects, implemented measures, third-party assurance results, etc.).
4. If every candidate is only a target declaration / restatement, no-data, unrelated, or all of them are 無資料, output 證據: 無資料 / 頁碼: 無資料.
5. Never output reasoning, <think>, preamble, closing remarks, explanations or ```markdown wrapping.

[Output format] (exactly two lines; keep the Chinese line labels 證據 and 頁碼 exactly as written)
證據: <verbatim quoted text or 無資料>
頁碼: <the matching page number or 無資料>
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
