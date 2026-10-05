# Promise-tracking dataset — schema and label scheme

The unit of work is one **commitment**: a target a company stated in report year
*d*, to be met by target year *t*. Every later report year between *d* and *t* is
one **commitment-year**, and each commitment-year gets a verdict. One commitment
plus all of its commitment-years is one annotation task.

Two stages produce the dataset:

| Stage | Produced by | Output |
|---|---|---|
| **A. Automatic** | the LLM pipeline (`openai_*.py`) | `result/<company>/summary.json` — extracted commitments, one verdict per commitment-year, the verbatim evidence sentence + its page, and up to 3 related clues + pages |
| **B. Human** | the annotation platform in this folder | three CSVs (`run.sh export`): the corrected verdicts, the evidence basis actually used, and a 0–100 achievement estimate per commitment-year |

Stage B never edits stage A in place: the AI's answer and the human's answer sit
side by side in every row, so "the AI was right", "the AI was wrong" and "the AI
was right for the wrong reason" are all recoverable.

## 1. Label scheme

Three axes are kept **separate**. Collapsing them into one label set is what makes
a promise-tracking dataset ambiguous, so they are annotated (or derived)
independently.

### Axis 1 — fulfilment, per commitment-year

The interim status in one report year, on the way to the target year.

| Machine label (stage A) | Human label (stage B) | English |
|---|---|---|
| `已達成` | `已達成` | Achieved |
| `部分達成` | `尚未達成` | Partially achieved / not yet achieved |
| — (cannot be produced) | `遠離目標` | Moving away from target (worse than the previous year) |
| `未提及` | `未提及` | Not mentioned in this year's report |

The pipeline has three labels; annotators have four. `遠離目標` is the label the
pipeline cannot produce and the one that carries most of the signal — a
regression is exactly the case the task is about. `部分達成` maps to `尚未達成`
whenever the two sides are compared (`common.DisplayStatus`), so the AI and human
columns are always on the same four-label scale.

### Axis 2 — evidence basis, per commitment-year

Not "did the model cite something" but "was what it cited the right thing".
Column `evidence_quality`:

| Value | Meaning |
|---|---|
| `ok` | the AI's verdict was confirmed as-is |
| `current` | the verdict was wrong, but the quoted evidence is still the right basis |
| `clue` | the correct basis is one or more of the same-year clues, not the quoted evidence |
| `mixed` | the quoted evidence **plus** one or more clues |
| `other` | none of the listed material; the annotator found other evidence in the report (and gives the page) |
| `none` | nothing in that year's report supports any verdict → `未提及` |
| `not_in_report` | **the sentence the AI quoted is not in that report at all** |

`evidence_ok` is the binary view (`1` for `ok`/`current`). "Evidence Found /
Not Found" in the proposal slides corresponds to
`evidence_quality not in ('none', 'not_in_report')` — a quotation that is not
in the report is not evidence that was found, so both values sit on the "not
found" side even though they mean different things.

`not_in_report` is deliberately a separate value from `none`, and the
distinction is the point of the axis. `none` is a fact about the **company** —
it disclosed nothing on this commitment that year. `not_in_report` is a fact
about the **model** — it produced a quotation that does not exist in the
document it cited. Collapsing the two, which is what happens when annotators
have no way to say the second, inflates the apparent non-disclosure rate and
hides the hallucination rate; both numbers are results this dataset is meant to
report. Rows carrying it also set `alt_clue`, so any same-year clue that *was*
the real basis is still recorded.

The value was added on 2026-09-18, part-way through the first collection round.
Answers given before then could not express it and may carry `none` where a
later annotator would have used `not_in_report`, so the two rates are not
comparable across that boundary.

Do **not** split on `submitted_at` to find the boundary: a revision keeps the
original `submitted_at` and only sets `edited_at`, so an answer first submitted
before the change and revised afterwards carries the new value under an old
date. The same caveat applies to `source_page_ok`. Where a row could express
the new value, the column itself says so — use the column's own emptiness, or
`COALESCE(edited_at, submitted_at)`, not `submitted_at` alone.

### The citation itself, per commitment

Separate again from all of the above: `source_page_ok` records whether the
commitment is actually on the page the pipeline cited. `wrong_page` is a
retrieval-precision problem — the commitment is real, the reference is off.
`not_found` means the extracted "commitment" has no basis in the report at all,
and it ends the item: there is nothing to verify year by year, so the yearly
columns are empty for those rows exactly as they are for `is_commitment =
not_commitment`. Reading `not_found` as a company non-disclosure would be
wrong; it is a pipeline hallucination rate.

### Axis 3 — verifiability, per commitment (derived, not annotated)

Whether the target year is already covered by the reports on hand. Column
`resolved` in `esg_risk.csv`: `1` when `target_year <= ` the last tracked report
year, `0` otherwise. A `resolved = 0` commitment is **not yet verifiable** — its
outcome label is censored, not negative. This is a deterministic function of two
dates, so it is derived rather than left to annotator judgement.

### The final outcome, per commitment

Three labels only, at the target year: `已達成` Achieved / `未達成` Not achieved /
`未提及` Not mentioned. Anything short of achieved (`部分達成`, `尚未達成`,
`遠離目標`) counts as `未達成` here (`common.FinalStatus`).

### Mapping to the proposal slides

| Slide label | Recovered from |
|---|---|
| Evidence Found / Not Found | `evidence_quality not in ('none', 'not_in_report')` (per commitment-year) |
| Achieved | `human_status == 已達成` |
| Partially Achieved | `human_status == 尚未達成` |
| Not Achieved | `human_status == 遠離目標`, or `human_final_status == 未達成` at the target year |
| Not Yet Verifiable | `resolved == 0` |

The mapping is lossless in this direction only: the four slide labels can always
be rebuilt from the released columns, but not the other way round. Releasing the
columns and the mapping lets every downstream task pick its own granularity.

## 2. Years

Years are ROC / Minguo years by default (ROC = Gregorian − 1911, so `109` = 2020)
because that is how the Taiwanese reports are numbered. `ESG_YEAR_STYLE=ad`
switches the platform to Gregorian years; the number in the CSV is whatever
`summary.json` carries.

## 2a. Output tokens the pipeline parsers depend on

Stage A parses the model's reply with plain string / Markdown-table parsers in
`openai_utils.py`, keyed on exact Chinese tokens. The prompt *instructions* can
be written in any language (the English edition does this), but the model must
still emit these tokens verbatim, or parsing fails silently. Complete list:

| Purpose | Token(s) the parser matches |
|---|---|
| Extraction table headers | `承諾` (commitment) / `發布年份` (declared year) / `目標年份` (target year) / `承諾來源頁碼` (source page) |
| Verification table headers | `達成狀態` (status) / `證據` (evidence) / `證據來源頁碼` (evidence page) / `相關線索` (clues) |
| Per-year status values | `已達成` (achieved) / `部分達成` (partially achieved) / `未提及` (not mentioned) |
| Empty-cell placeholders | `無資料` / `無` (no data) |
| Clue separator inside one cell | `<br>` |
| Evidence-picker line labels | `證據:` (evidence) / `頁碼:` (page) |

The page note `（第N頁）` ("page N") inside clue text is a convention the prompt
asks for but nothing parses; clue strings are carried into `summary.json`
verbatim. See `PROMPTS_EN.md` in the English edition for the prompts themselves.

## 2b. Page numbers: printed page vs PDF page

The pipeline was asked for the page number **printed on the report page**, but
also for a number inside the PDF-index range of the chunk it was reading, so
its cited pages are a mix: mostly printed numbers (one or two short of where a
PDF viewer opens the page on a report with a cover leaf), sometimes PDF indices
already. The released numbers are **PDF page indices**, converted with
`page_shift.py`: `measure` finds each report file's offset (the mode of "where
the quoted sentence really is minus the page cited", with the numbers printed
on the pages as a cross-check), and `apply` moves **every cited page of that
report** — the commitment's source page, every year's evidence page and the
page references inside the clue texts — by that offset (`--mode uniform`, the
default: the whole report moves together, the pipeline's numbers being the
printed ones). `--mode verify` instead looks each quoted sentence up first and
keeps a citation that is already on the cited PDF page. A report whose offset
could not be established (too few quotes found, a scanned file with no text
layer, quotes and printed numbers disagreeing) is held back until someone
checks it by eye and records the offset with `page_shift.py set <company>
<year> <offset>`, or `apply --include_flagged` is used. Each converted field
carries its decision:

| Column | File | Meaning |
|---|---|---|
| `source_page` / `evidence_page` | both | the released (PDF-index) page |
| `source_page_orig` / `evidence_page_orig` | both | the number as the pipeline wrote it |
| `source_page_seen` / `evidence_page_seen` | both | the number **this annotator had in front of them** when answering, recorded with the answer itself (the task page carries the cited pages in hidden fields). A revision keeps the numbers recorded with the judgement it revises unless that judgement — the source-page check, or a year's basis / status / page — itself changed, in which case the pages shown at the revision are recorded. Answers written before the platform recorded this were filled in by `page_shift.py apply` from the values stored at that moment (nothing had moved yet), so every released row carries it; `alt_clue_text` likewise shows the clue texts as they were when the answer was made against the original pages |
| `source_page_offset` / `page_offset` | both | released − original (for the first page when a field cites several; the per-page decisions are in `summary.json`) |
| `source_page_kind` / `evidence_page_kind` | both | `uniform` = moved by the report's offset (the default: every page of a report moves together, the pipeline's numbers being the printed ones); in `--mode verify` runs instead: `pdf` = the **whole** quoted sentence was found on the cited page and nowhere else, so the number was already a PDF index (kept); `printed` = the whole quote was found at cited + offset and not on the cited page (moved); `unsure` = only a fragment of the quote was found on a candidate page, or the whole quote is on both — no proof either way, so the report's offset was applied (worth a look by hand); `wrong` = the quote is elsewhere (moved; the citation is simply wrong); `assumed` = the quote was not found (moved); `unknown` = not found and left untouched (`--keep_unverified`, which also keeps `unsure` and `wrong`). In both modes: `out_of_range` = the shift would leave the file at either end (kept); `held` = the report's offset could not be established, field untouched; empty = never converted. Every decision record also carries `d`, the delta actually applied (0 = kept), which is authoritative |

`source_page_ok` / `source_page_fix` / `correct_page` are judgements about
`*_seen`, not about the released page: an annotator who wrote "wrong page, it
is on 85" against a cited 84 that was later converted to 85 was right about
what they saw. Pages the annotators typed are left exactly as typed; they
were asked for the PDF viewer's number, but some copied the printed one, so a
typed value one or two below the released AI page is usually the same page.
`summary.json` keeps the originals beside the converted values
(`source_page_orig`, `evidence_page_orig`, `clues_orig`) and every decision,
with the conversion time, under a `page_shift` record. Page lists inside a
clue (`（第12、13頁）`, `(p.12,13)`, `p. 12; 13`) are read and shifted whole,
but only when the whole list reads as a page list: a decimal or a
thousands-separated figure after the reference (`(p.12,30.5% less)`,
`(p.12,30,000 tonnes)`) is never touched. A range (`p.12-14`) contributes its
two ends only and is not expanded.

## 3. `esg_item.csv` — one row per (commitment, annotator)

| Column | Source | Meaning |
|---|---|---|
| `item_id` | — | commitment id, stable across the three files |
| `company`, `industry` | input | company folder name; industry from `industry.json` |
| `commitment` | A | the commitment text as worded in the report |
| `declared_year`, `target_year` | A | stated in / due by |
| `source_page` | A | page of the declaring report where the commitment appears, as the AI read it (PDF page index after the conversion in §2b) |
| `source_page_orig`, `source_page_seen`, `source_page_offset`, `source_page_kind` | derived | the original number, the number this annotator saw, released − original, and how it was converted (see §2b) |
| `source_page_ok` | B | did the annotator find the commitment there? `ok` / `wrong_page` (it is elsewhere in the report) / `not_found` (it is nowhere in the report, which ends the item). Empty = no page was cited, or the item was answered before the check existed (2026-09-18) |
| `source_page_fix` | B | the page it is really on, when `wrong_page` and the annotator noted it (optional) |
| `ai_final_status` | A | the AI's overall conclusion (three labels) |
| `ai_seq` | A | the AI's per-year verdicts, `year:status …` |
| `ai_letters` | A | the same sequence as `A`/`P`/`F`/`N`, for eyeballing |
| `priority`, `flags` | A | anomaly score and the rules that fired (see §6) |
| `stratum` | A | `anomaly` (flagged) or `control` (uniformly sampled) |
| `is_gold` | A | `1` = shared calibration item, answered by every annotator |
| `annotator` | B | anonymous annotator label (`A01`, …) in the shared copy |
| `is_commitment` | B | `valid` / `not_commitment` / `no_target` / `unsure` — is this a verifiable commitment at all. `no_target` (added 2026-10-03): it reads like a commitment but the report names no year by which it is to be met, so the pipeline's target year is a guess; like `not_commitment` it ends the item (yearly columns empty). Earlier answers could not express it and may carry `not_commitment`, `unsure` or a `window_ok = target_wrong` instead |
| `not_reason` | B | why not, when `not_commitment` (required) or `no_target` (optional) |
| `commitment_fix` | B | corrected commitment wording, when the extraction was sloppy |
| `target_year_fix` | B | corrected target year, when the extraction misread it |
| `human_final_status` | B | the human overall conclusion (three labels) |
| `ai_final_agree` | derived | `1` when the two overall conclusions match |
| `window_ok` | B | tracking-window check: `ok` / `target_wrong` / `missing_years` / `extra_years` / `unsure` |
| `correct_target_year` | B | the right target year when `window_ok = target_wrong` |
| `n_year_flips` | derived | commitment-years where the human status differs from the AI's |
| `comment` | B | free-text note (usually empty) |
| `n_page_views` | B | times the annotator opened the cited report pages |
| `seconds` | B | wall-clock from opening the task to submitting |
| `active_seconds` | B | time actually spent in the tab (hidden, unfocused and idle stretches excluded) |
| `submitted_at`, `n_edits`, `edited_at` | B | submission and later revisions (UTC) |
| `starred`, `note` | B | the annotator's own star (`1`/`0`) and free-text note on the item — a working aid ("unsure, come back to this"), saved separately from the answer and never required. Empty/`0` for every answer made before 2026-10-03 |

## 4. `esg_year.csv` — one row per (commitment, year, annotator)

The correction record proper.

| Column | Source | Meaning |
|---|---|---|
| `item_id`, `company`, `industry`, `commitment`, `declared_year`, `target_year` | — | commitment context, repeated for convenience |
| `year` | A | the report year this verdict is about |
| `years_to_target` | derived | `target_year − year` |
| `ai_status` | A | the AI's verdict, on the four-label scale |
| `human_status` | B | the human verdict (`= ai_status` when confirmed) |
| `changed` | derived | `1` when the two differ |
| `evidence_quality`, `evidence_ok` | B | axis 2 above |
| `achieve_prob` | B | 0–100: how likely the target-year record is to be met, judged from what is known up to this year |
| `achieve_prob_num` | derived | numeric view (legacy `low`/`mid`/`high` rows mapped to 0/1/2) |
| `correct_page` | B | page(s) supporting the human verdict, when it is not "not mentioned" |
| `alt_clue` | B | every basis the annotator ticked (`current`, clue indices, `none`) |
| `alt_clue_text` | derived | the clue sentences those indices point at |
| `custom_basis` | B | free-text basis, when the annotator found something not listed |
| `evidence`, `evidence_page` | A | the sentence the AI quoted and its page (PDF page index after the conversion in §2b) |
| `evidence_page_orig`, `evidence_page_seen`, `page_offset`, `evidence_page_kind` | derived | the original number, the number this annotator saw, released − original, and how it was converted (see §2b) |
| `is_gold` | A | calibration item |
| `annotator`, `assign_id` | B | anonymous label; `assign_id` groups one person's rows for one commitment |

`achieve_prob` is the forward-looking series the risk model trains on: each year's
slider starts at the previous year's value, so an untouched slider means "nothing
changed this year", and the drift across years is the trajectory.

## 5. `esg_risk.csv` — one row per commitment (majority vote)

Aggregated across annotators: features → probability trajectory → verified outcome.

| Column | Meaning |
|---|---|
| `item_id`, `stratum`, `is_gold`, `company`, `industry`, `commitment` | as above |
| `declared_year`, `target_year`, `horizon_years` | `target_year − declared_year` |
| `resolved` | axis 3: `1` = the target year is covered by the reports, so the outcome is final |
| `n_years_tracked`, `has_number`, `commitment_len` | commitment features |
| `n_annotators` | people who submitted this commitment (calibration items carry the most) |
| `prob_trajectory` | `year:prob>year:prob>…`, per-year **median** of the annotators' 0–100 estimates |
| `prob_first`, `prob_last`, `prob_trend` | first, last, and last − first |
| `is_commitment_majority` | majority vote on "is this a verifiable commitment" |
| `human_outcome` | majority vote on the overall conclusion (three labels) |
| `outcome_agreement` | share of annotators behind that majority (1.0 = unanimous) |
| `ai_final_status`, `ai_letters` | the AI's conclusion and per-year letter sequence |
| `ai_vs_human_final` | `1` when the AI's conclusion matches the human majority |
| `n_year_flips` | total commitment-years corrected, across annotators |
| `priority`, `flags` | anomaly score and the rules that fired |

## 6. Sampling and the anomaly score

Annotator time is finite, so items are served worst-looking first, but the pool is
**not** all anomalies: `ingest.ScorePromise` gives every commitment a penalty
score from ten rules, and an item with at least one rule firing goes to the
`anomaly` stratum, the rest to `control`. Control items are sampled uniformly, so
the released data still carries a realistic base rate of ordinary commitments.

The rules, by weight: an `已達成` year followed by a non-`已達成` year (the pattern
the whole study is about); A→P→A→P oscillation; the same evidence sentence graded
differently in different years; `已達成` backed by a sentence that only restates
the goal; `已達成` contradicted by its own clues; `已達成` with no evidence text at
all; an overall conclusion that disagrees with the last observed year; a year
going silent after a year that had content; and a target year earlier than the
declaration year (a year-parsing bug).

The top `ESG_CALIBRATION_N` items by that score are the **shared calibration
set**: every annotator answers them, in the same order, before anything else, so
inter-annotator agreement is measurable on identical material. They are exempt
from the redundancy cap but are otherwise ordinary items — they appear in all
three CSVs with `is_gold = 1`, and because everyone answers them they carry the
most annotators per item, so their majority vote is the firmest in the set.

## 7. De-identification

The platform stores a participant id per annotator; in this deployment that id is
the person's only credential. Any copy that leaves the collecting machine must
replace it with an anonymous label (`annotator` = `A01`, `A02`, …), stable across
the three files. The `is_test` accounts used for self-checks are excluded from all
three exports.
