# Prompts — English reference

The pipeline sends six prompts to the model (all defined in `openai_utils.py`).

- In **this folder** (the English edition), the prompts in `openai_utils.py` instruct
  the model **in English**, following the translations in this document.
- In **`../chinese/`** (the original edition), the same six prompts are written in
  Chinese.

Both editions demand the **same output schema** from the model — Traditional Chinese
Markdown tables — so their results are interchangeable. This file documents each
prompt (function, stage, placeholders, instructions, required output) and, most
importantly, the output tokens that must never be translated.

**This file is documentation only — the code does not load it.**

## Why the model's OUTPUT must stay Chinese

The model's *output* is parsed by plain string/Markdown parsers in `openai_utils.py`,
which key off exact Chinese tokens. Whatever language the prompt instructions use,
every one of these output tokens must remain **in Chinese**, or parsing breaks:

| Token (must stay Chinese) | Where it is required |
|---|---|
| Table headers `承諾` / `發布年份` / `目標年份` / `承諾來源頁碼` | `ParseExtractionTable`, `CommitmentsToMarkdown` |
| Table header `達成狀態` / `證據` / `證據來源頁碼` / `相關線索` | `ParseVerificationTable` |
| Status values `已達成` / `部分達成` / `未提及` | `AggregatePerCommitment`, status ranking |
| Placeholders `無資料` / `無` | clue/evidence emptiness checks (`_CLUE_EMPTY`) |
| Clue separator `<br>` | `CluesToCell` / `_SplitClues` |
| Page notation `（第N頁）` inside clue text | human-readable convention (not parsed, but expected) |
| Line labels `證據:` / `頁碼:` (evidence picker, prompt 6) | `_EVIDENCE_LINE` / `_PAGE_LINE` regexes |

Two further rules the English-edition prompts enforce explicitly:

- **Commitment text is never translated.** Cell content (commitment wording, quoted
  evidence, clues) is Traditional Chinese, taken from the report. In the verification
  and evidence-picker prompts the commitment text must be copied **character for
  character** from the input list — `_MatchesCanonical` matches rows back to the
  canonical list by that text.
- `{...}` placeholders (e.g. `{year_x}`, `{start_page}`) are filled in at runtime.
  In the Chinese edition `{future_hint}` is a short Chinese phrase; in the English
  edition it is the equivalent English phrase (*"must be ≥ ROC year N"*, or *"must be
  greater than the declared year N"* when N isn't numeric).

---

## 1. Extraction prompt

- **Function:** `_ExtractAllPromisesPrompt` → used by `ExtractPromisesFromChunk` /
  `ExtractModelPromises`
- **Stage:** 1 (get_promise)
- **Placeholders:** `{start_page}`, `{end_page}`, `{year_x}` (report/declared year),
  `{future_hint}`, `{example_year}`

**Instructions (as embedded in this edition; a translation of the Chinese original):**

> You are a professional ESG report analyst.
> The attachment is an ESG report PDF (these pages correspond to pages
> {start_page}–{end_page} of the original report; the report's publication year is ROC
> year {year_x}).
> Your task is to extract, **completely and without omission**, every commitment in
> this attachment that explicitly states a "specific future target year", and to label
> that target ROC year.
>
> **[Rules]**
> 1. List **all** qualifying commitments in the attachment, item by item — do not omit
>    or abbreviate. If one page has several commitments, output several rows.
> 2. The target year must be in the **future**: strictly greater than the publication
>    year {year_x} ({future_hint}). Any commitment whose target year equals or precedes
>    {year_x} (already due, or current-year) is excluded.
> 3. Only extract commitments whose target year is **explicit, single, and a ROC-year
>    number**; vague ones — "未來" (future), "長期" (long-term), "中長期" (mid-to-long
>    term) — with no concrete year are excluded.
> 4. If a commitment's text is a range (e.g. "{year_x}-{example_year}",
>    "{year_x}~{example_year}"), take only the farthest year; if it lists several years,
>    split into several rows, and every row's year must satisfy Rule 2.
> 5. Exclude awards, rankings, certifications, ratings and other external recognition;
>    list only concrete actions, projects or targets the company itself undertakes.
> 6. Keep the `承諾` (commitment) field to the concise core; do not write the year in
>    the description. Keep the commitment text in Traditional Chinese, as worded in
>    the report — do NOT translate it into English.
> 7. `發布年份` (declared year) is always {year_x}.
> 8. `目標年份` (target year) is a concrete ROC-year number and must be > {year_x}.
>    If the report states a Gregorian year, convert it to the ROC year
>    (ROC = Gregorian − 1911; e.g. 2030 → 119) and output the ROC number only.
> 9. `承諾來源頁碼` (source page) must be a single page number between {start_page} and
>    {end_page} (by the report's printed page number).
> 10. If no qualifying commitment exists, output **nothing at all** (not even the table
>     frame or header).
> 11. Do not output reasoning, `<think>` tags, explanations, or ```` ```markdown ````
>     wrapping; output the pure table only, with the exact Chinese headers below.

**Required output format** (headers stay Chinese):

```
| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | {year_x} | (ROC year, > {year_x}) | (page) |
```

---

## 2. Verification prompt

- **Function:** `_VerifyPrompt` → used by `VerifyChunk` / `VerifyChunksForModel`
- **Stage:** 3 (check_promise)
- **Placeholders:** `{start_page}`, `{end_page}`, `{promises_md}` (the canonical
  commitment list being checked)

**Instructions (as embedded in this edition; a translation of the Chinese original):**

> You are a strict ESG auditor.
> The attachment is an ESG report PDF (these pages correspond to pages
> {start_page}–{end_page} of the original report).
> Your task is to check whether the attachment contains **concrete evidence of
> achievement, or related clues**, for the following [list of prior-report commitments].
>
> **[List of prior-report commitments]**
> {promises_md}
>
> **[Output ONLY commitments that have evidence or clues — most important]**
> - Output **only** the rows where, within these pages, the commitment "has concrete
>   evidence of achievement" or "has a related clue".
> - For any commitment with no related content at all in these pages (neither evidence
>   nor a clue), do **not** output a row for it; you do not need to list `未提及` (not
>   mentioned) commitments that have no clue.
> - If none of the commitments have evidence or clues in these pages, output **nothing
>   at all** (not even a header or table frame).
> - Do not add commitments that aren't on the list; copy the commitment text
>   **character for character** from the list — do not rephrase, translate or
>   abbreviate it.
>
> **[Audit rules]**
> 1. `達成狀態` (status) may only be one of exactly these three Chinese labels:
>    `已達成` (achieved), `部分達成` (partially achieved), `未提及` (not mentioned).
>    - `已達成` / `部分達成`: you must quote the original (Chinese) text from the
>      attachment **exactly** in the `證據` (evidence) column; do not paraphrase or
>      translate.
>    - `未提及`: use only for a commitment listed because it "has no concrete evidence
>      but has a related clue"; then set `證據` (evidence) and `證據來源頁碼` (evidence
>      page) to `無資料` (no data), and put the related snippet in the `相關線索`
>      (related clues) column.
> 2. **[Most important]** "Merely restating the commitment" is **not** evidence: if the
>    attachment only re-declares the same target, repeats the commitment, or expresses
>    resolve / vision / slogans with nothing actually delivered, it must **not** be
>    judged `已達成` or `部分達成`.
>    Evidence must be **concrete, complete data or actual accomplishments** — e.g.
>    actually-achieved figures or percentages, completed or in-progress projects,
>    measures already implemented, third-party assurance/verification results: content
>    that substantiates "what has actually been done".
>    If the attachment has only a target declaration / restatement for that commitment
>    with no delivered data or facts, do **not** judge it `已達成` or `部分達成`; you may
>    instead list it as `未提及` and put that declaration sentence into `相關線索` for
>    human review.
> 3. `證據來源頁碼` (evidence page) must be a single page number between {start_page}
>    and {end_page} (by printed page number).
> 4. `相關線索` (related clues) column (a human-review aid; does not affect the status):
>    list original snippets from the attachment that are "related to the commitment's
>    topic or metric but not enough on their own to conclude achievement", **up to 3**.
>    - When to use: mentions the same topic/metric but with no concrete progress or
>      number; slogans only; the number's direction or range doesn't fully match;
>      suspected but not certain to be the same thing.
>    - Even if this row's `證據` column is already filled, if there are other related or
>      suspected snippets, list them here too, so a reviewer has more to work with.
>    - Each item must be text that **truly exists** in the attachment (quoted in the
>      original Chinese); do not fabricate or rewrite. Mark the page after each in the
>      form `（第N頁）`, e.g. "…（第12頁）".
>    - Separate multiple items with `<br>` (e.g.
>      snippet1（第3頁）`<br>`snippet2（第5頁）`<br>`snippet3（第8頁）).
>    - Do not repeat a sentence already placed in the `證據` column.
>    - An `已達成` / `部分達成` row with no extra snippet may put `無` (none) here; but a
>      row listed as `未提及` must **not** be `無` (there must be the clue that made you
>      list it).
> 5. Do not output reasoning, `<think>` tags, explanations, or ```` ```markdown ````
>    wrapping; output the pure table only (or blank when nothing matches), with the
>    exact Chinese headers below.

**Required output format** (headers + status values stay Chinese):

```
| 承諾 | 達成狀態 | 證據 | 證據來源頁碼 | 相關線索 |
|---|---|---|---|---|
| (commitment with evidence or clue) | (已達成/部分達成/未提及) | (quoted text / 無資料) | (page / 無資料) | (up to 3 related snippets + page, joined by <br> / 無) |
```

---

## 3. Same-year consolidation prompt

- **Function:** `DedupAllPromisesWithText`
- **Stage:** 1 (get_promise) — merges the per-model extractions from the **same**
  report year
- **Placeholders:** `{year_x}`, `{future_hint}`, `{sources}` (the per-model tables)

**Instructions (as embedded in this edition; a translation of the Chinese original):**

> You are a senior ESG data curator. Below are commitment lists that AI models
> extracted from the **same** ROC year {year_x} ESG report (each carries an explicit
> target ROC year).
>
> **[Task]**
> 1. **Semantic dedup:** merge commitments describing the same thing. But if the target
>    years differ, treat them as different items even if the wording is similar — never
>    merge them.
> 2. **Keep everything:** except for truly semantically-duplicate items, do not delete
>    or omit any commitment; list them all.
> 3. **Filter noise:** remove awards, rankings, certifications, ratings and other
>    external recognition, or items clearly not undertaken by the company itself.
> 4. **Target year must be in the future:** keep only commitments whose target year is
>    strictly greater than the declared year {year_x} ({future_hint}); remove any with
>    target year ≤ {year_x}.
> 5. **Unify the description:** after merging, pick the most concise, clearest wording,
>    in the original Traditional Chinese (never translate); do not write the year in
>    the commitment text.
> 6. `發布年份` (declared year) is always {year_x}.
> 7. `目標年份` (target year) keeps the original ROC-year number (and must be >
>    {year_x}).
> 8. `承諾來源頁碼` (source page) keeps the smallest (earliest) page on merge; if all are
>    `無資料`, use `無資料`.
> 9. Do not output reasoning, `<think>`, preamble, closing, or ```` ```markdown ````
>    wrapping; output the final table only, with the exact Chinese headers below.
>
> **[Extracted commitment lists]**
> {sources}

**Required output format:**

```
| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | {year_x} | (ROC year, > {year_x}) | ... |
```

---

## 4. Cross-year consolidation prompt (fallback)

- **Function:** `DedupCrossYearPromisesWithText`
- **Stage:** 3 (check_promise) — only on the **fallback** path, when the pre-built
  `promise/{target}.md` files from Stage 2 are missing
- **Placeholders:** `{year_floor}` (the year being checked), `{sources}` (per-year
  tables)

**Instructions (as embedded in this edition; a translation of the Chinese original):**

> You are a senior ESG data curator. Below are commitment lists mentioned by one or
> more of the company's ESG reports of different publication years. These commitments'
> target years are not necessarily the same (all ROC year {year_floor} or later);
> produce one unique commitment list.
>
> **[Task]**
> 1. **Semantic dedup:** merge commitments describing the same thing (e.g. "cut carbon
>    10%" and "reduce carbon emissions 10%" are the same). But if the **target years
>    differ**, treat them as **different items** even if the wording is similar — never
>    merge.
> 2. **Keep everything:** except for items that are **same target year AND semantically
>    duplicate**, do not delete or omit any commitment; list every commitment and every
>    distinct target year separately.
> 3. **Unify the description:** after merging, pick the most concise, clearest wording,
>    in the original Traditional Chinese (never translate); do not write the year in
>    the commitment text.
> 4. `發布年份` (declared year) keeps the **earliest** ROC year among all source reports
>    that mention this commitment.
> 5. `目標年份` (target year) keeps the commitment's original ROC-year number; do not
>    rewrite it or unify years.
> 6. `承諾來源頁碼` (source page) keeps the earliest source report's page; if that report
>    lists the commitment on several pages, take the smallest.
> 7. No reasoning, `<think>`, preamble, closing, or ```` ```markdown ```` wrapping;
>    output the pure table only, with the exact Chinese headers below.
>
> **[Per-year extracted commitment lists]**
> {sources}

**Required output format:**

```
| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | (earliest ROC year) | (the commitment's original target ROC year) | ... |
```

---

## 5. Single-target consolidation prompt

- **Function:** `DedupSingleTargetPromisesWithText`
- **Stage:** 2 (consolidate_promises) — run once per target year to fix one stable
  wording that every later year reuses
- **Placeholders:** `{target_year}`, `{sources}` (per-declaring-year tables)

**Instructions (as embedded in this edition; a translation of the Chinese original):**

> You are a senior ESG data curator. Below are commitment lists — all with **target
> year = ROC year {target_year}** — mentioned by one or more of the company's ESG
> reports of different publication years. Produce one unique commitment list.
>
> **[Task]**
> 1. **Semantic dedup:** merge commitments describing the same thing (e.g. "cut carbon
>    10%" and "reduce carbon emissions 10%" are the same). Except for truly
>    semantically-duplicate/identical items, do not delete or omit any commitment; list
>    them all.
> 2. **Unify the description:** after merging, pick the most concise, clearest wording
>    as the **single standard description**, in the original Traditional Chinese
>    (never translate); do not write the year in the text. Assume every future year
>    will reuse this wording, so make it clear, stable and reusable.
> 3. `發布年份` (declared year) keeps the **earliest** ROC year among all source reports
>    that mention this commitment.
> 4. `目標年份` (target year) is always {target_year}.
> 5. `承諾來源頁碼` (source page) keeps the earliest source report's page; if that report
>    lists it on several pages, take the smallest.
> 6. No reasoning, `<think>`, preamble, closing, or ```` ```markdown ```` wrapping;
>    output the pure table only, with the exact Chinese headers below.
>
> **[Per-year extracted commitment lists]**
> {sources}

**Required output format:**

```
| 承諾 | 發布年份 | 目標年份 | 承諾來源頁碼 |
|---|---|---|---|
| ... | (earliest ROC year) | {target_year} | ... |
```

---

## 6. Best-evidence picker prompt

- **Function:** `PickBestEvidenceWithText`
- **Stage:** 3 (check_promise) — chooses one evidence snippet per commitment when
  several chunks/models produced candidates (skipped in code when there's only one)
- **Placeholders:** `{year_y}` (target/checked year), `{commitment}`,
  `{candidates_block}`

**Instructions (as embedded in this edition; a translation of the Chinese original):**

> You are a strict ESG auditor. Below are candidate pieces of evidence found across the
> pages of the ROC year {year_y} ESG report for **the same commitment**. Pick the single
> one that **most directly and precisely substantiates** the commitment.
>
> **[Commitment]**
> {commitment}
>
> **[Candidate evidence]**
> {candidates_block}
>
> **[Selection rules]**
> 1. You must pick one of the candidates above; do not write or rewrite text yourself —
>    copy the original verbatim.
> 2. The page must be the page number of the chosen candidate; do not use another
>    candidate's page.
> 3. "Merely restating the commitment" is not evidence: re-declaring the target,
>    repeating the commitment, expressing resolve / vision / slogans do not count. Prefer
>    candidates with **concrete, complete data or actual accomplishments** (real
>    figures/percentages, completed or in-progress projects, implemented measures,
>    third-party assurance results, etc.).
> 4. If all candidates are only target declarations / restatements, no-data, unrelated,
>    or all `無資料`, output `證據: 無資料` / `頁碼: 無資料`.
> 5. No reasoning, `<think>`, preamble, closing, explanation, or ```` ```markdown ````
>    wrapping.

**Required output format** — exactly two lines; the `證據:` / `頁碼:` line labels are
matched by regex and **must stay Chinese**:

```
證據: <quoted text or 無資料>
頁碼: <page or 無資料>
```
