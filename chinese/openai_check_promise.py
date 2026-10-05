"""Verify whether prior-year commitments targeting year Y were achieved in the
Y-year ESG report (OpenAI, PDF-file based).

Usage:
    python openai_check_promise.py --company <NAME> --year <ROC_YEAR>
                                   [--pdf_dir PDF_ROOT]
                                   [--image_dir IMAGE_ROOT]
                                   [--result_dir RESULT_ROOT]
                                   [--report_path PATH]

Pipeline:
  1. Assemble the canonical commitment list targeting {year} or later. Preferred
     source: the pre-consolidated {result_dir}/{company}/promise/{T}.md files (one
     per target year T, built by openai_consolidate_promises.py) for every T >=
     year — this guarantees a promise reads with the SAME wording in each year
     that tracks it. Pulling in later-target commitments (not just == year) is
     what lets us catch ones fulfilled EARLY: their evidence appears in THIS
     year's report even though the target is still ahead. Fallback (when promise/
     is absent): walk {result_dir}/{company}/<x>/promise.md for numeric x < year,
     keep rows whose 目標年份 >= year, then run one judge consolidation pass over
     them (distinct target years kept apart).
     Either way the assembled list is then filtered to commitments whose
     發布年份 < year AND 目標年份 >= year, so only prior-report promises still in
     flight are verified against year {year}'s report (a promise first declared in
     {year} itself has nothing to verify yet; its check starts the next year).
  2. Send the {year} report PDF to the vision model as overlapping PDF chunks
     (20 pages, 2-page overlap) and verify the canonical list chunk by chunk.
  3. Code-based aggregation per commitment (any 已達成 -> 已達成; all 未提及 ->
     未提及; otherwise 部分達成); related/suspicious clues are gathered here too,
     purely in code. For non-未提及 commitments the judge model picks the single
     best evidence — skipped (no LLM) when only one candidate exists. The result
     8-column table (final column 其他相關/可疑證據 lists deduped related/suspicious
     snippets per commitment for human review) is written to
     {result_dir}/{company}/{year}/check_result.md. This contains commitments
     targeting {year} AND later years.

--image_dir is accepted for CLI parity with the image-based pipelines but is
unused in this PDF-file flow.
"""

import argparse
import os
import sys
import traceback
from collections import Counter
from typing import Dict, List

from openai_utils import (
    AggregatePerCommitment,
    CHUNK_OVERLAP,
    CluesToCell,
    CommitmentsToMarkdown,
    CreateOpenaiClient,
    DedupCrossYearPromisesWithText,
    JUDGE_MODEL,
    LoadCanonicalPromisesForYear,
    PAGES_PER_CHUNK,
    ParseExtractionTable,
    PickBestEvidenceWithText,
    SanitizeCell,
    SourceBeforeYear,
    TargetsYearOrLater,
    VISION_MODELS,
    VerifyChunksForModel,
)


def CollectPriorPromises(
    result_dir: str, company: str, year: str,
) -> Dict[str, str]:
    """Scan {result_dir}/{company} for sibling year directories, read their
    promise.md, keep rows whose 目標年份 >= year.

    Collecting later-target commitments too (not just == year) is what lets the
    pipeline catch ones fulfilled EARLY: their evidence surfaces in THIS year's
    report even though their target year is still ahead.

    Returns {x_year_label: 4-col markdown table of filtered rows}. Sources that
    yield no matching rows are skipped.
    """
    base = os.path.join(result_dir, company)
    if not os.path.isdir(base):
        return {}

    try:
        year_int = int(year)
    except ValueError:
        raise ValueError(f"--year must be a ROC year integer, got: {year!r}")

    out: Dict[str, str] = {}
    for entry in sorted(os.listdir(base)):
        try:
            x = int(entry)
        except ValueError:
            continue
        if x >= year_int:
            continue
        promise_path = os.path.join(base, entry, "promise.md")
        if not os.path.isfile(promise_path):
            continue
        with open(promise_path, "r", encoding="utf-8") as f:
            md = f.read()
        rows = ParseExtractionTable(md)
        relevant = [r for r in rows
                    if TargetsYearOrLater(r.get("year_y", ""), year)]
        if relevant:
            out[entry] = CommitmentsToMarkdown(relevant)
    return out


def Main():
    parser = argparse.ArgumentParser(
        description="Verify Y-year ESG report against prior commitments targeting "
                    "Y (OpenAI, PDF-file based)."
    )
    parser.add_argument("--company", required=True,
                        help="Company folder under pdf_dir / result_dir.")
    parser.add_argument("--year", required=True,
                        help="Year being checked (民國年, e.g. 114).")
    parser.add_argument("--pdf_dir", default="pdf",
                        help="Root holding {company}/{year}.pdf (default: pdf).")
    parser.add_argument("--image_dir", default="image",
                        help="Unused in PDF-file mode; kept for CLI parity.")
    parser.add_argument("--result_dir", default="result",
                        help="Root for promise.md inputs + check_result.md output (default: result).")
    parser.add_argument("--report_path", default=None,
                        help="Explicit PDF path override; bypasses pdf_dir/company/year.")
    args = parser.parse_args()

    pdf_path = args.report_path or os.path.join(
        args.pdf_dir, args.company, f"{args.year}.pdf")
    out_path = os.path.join(
        args.result_dir, args.company, str(args.year), "check_result.md")

    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"Report PDF not found: {pdf_path}")

    client = CreateOpenaiClient()

    try:
        print(f"\n=== check-promise ({args.company} target year {args.year}) ===")
        print(f"  pdf:           {pdf_path}")
        print(f"  output:        {out_path}")
        print(f"  judge model:   {JUDGE_MODEL}")
        print(f"  vision models: {VISION_MODELS}")
        print(f"  chunk:         {PAGES_PER_CHUNK} pages, {CHUNK_OVERLAP}-page overlap")

        # ----- Step 1: assemble the canonical commitment list (target >= year) ---
        # Prefer the pre-consolidated promise/{T}.md files (built once per target
        # year by openai_consolidate_promises.py) so every promise reads with the
        # SAME wording in each year that tracks it. Fall back to collecting the
        # per-year promise.md and consolidating on the fly when promise/ is absent.
        print(f"\n[Step 1] Assembling canonical commitments targeting year "
              f"{args.year} (or later)...")
        canonical_md = LoadCanonicalPromisesForYear(
            args.result_dir, args.company, args.year)
        if canonical_md:
            print("  -> using pre-consolidated promise/*.md "
                  "(openai_consolidate_promises).")
            print(canonical_md)
        else:
            print("  [Info] No promise/*.md canonical files found; falling back to "
                  "per-year promise.md collection + on-the-fly consolidation.")
            per_source = CollectPriorPromises(
                args.result_dir, args.company, args.year)
            if not per_source:
                print(f"[Warn] No prior promise.md targeting year {args.year} found "
                      f"under {os.path.join(args.result_dir, args.company)}. Nothing "
                      f"to verify. Run openai_get_promise.py on earlier years first.")
                return
            print(f"  -> {len(per_source)} prior reports contributed: "
                  f"{sorted(per_source.keys())}")
            for src, md in per_source.items():
                print(f"--- 民國 {src} 年 (filtered to target {args.year}) ---")
                print(md)
            print(f"\n  Consolidation pass with judge model ({JUDGE_MODEL}) to merge "
                  f"semantically similar / identical commitments across years "
                  f"(distinct target years kept apart)...")
            canonical_md = DedupCrossYearPromisesWithText(
                client, per_source, args.year)
            print("\n--- Canonical commitment list (post-consolidation) ---")
            print(canonical_md)

        canonical_commitments = ParseExtractionTable(canonical_md)
        if not canonical_commitments:
            print("\n[Warn] Canonical list is empty. Stopping.")
            return

        # Keep only commitments DECLARED in an earlier report (發布年份 < year) whose
        # target is this year or later (目標年份 >= year). We verify PRIOR-report
        # promises against year Y's report: a promise first stated in Y's own
        # report has nothing to verify yet, and an already-expired target (< Y) is
        # out of scope. Rebuild canonical_md from the kept rows so the verify pass
        # only sees in-scope commitments.
        filtered = [
            r for r in canonical_commitments
            if SourceBeforeYear(r.get("year_x", ""), args.year)
            and TargetsYearOrLater(r.get("year_y", ""), args.year)
        ]
        dropped = len(canonical_commitments) - len(filtered)
        if dropped:
            print(f"  -> dropped {dropped} commitment(s) outside "
                  f"(發布年份 < {args.year} 且 目標年份 >= {args.year})")
        canonical_commitments = filtered
        if not canonical_commitments:
            print(f"\n[Warn] No commitment satisfies 發布年份 < {args.year} 且 "
                  f"目標年份 >= {args.year}. Nothing to verify.")
            return
        canonical_md = CommitmentsToMarkdown(canonical_commitments)
        print(f"  -> {len(canonical_commitments)} canonical commitments "
              f"(發布年份 < {args.year}, 目標年份 >= {args.year})")

        # ----- Step 2: per-model verification against the report PDF chunks -----
        print(f"\n[Step 2] Per-model verification of "
              f"{len(canonical_commitments)} canonical commitments...")
        all_chunk_results: List[Dict[str, str]] = []
        for model_name in VISION_MODELS:
            chunks = VerifyChunksForModel(
                client, model_name, pdf_path, canonical_md,
            )
            print(f"  [{model_name}] -> {len(chunks)} raw chunk rows")
            all_chunk_results.extend(chunks)

        # ----- Step 3: aggregate + pick best evidence per commitment -----
        print("\n[Step 3] Aggregating chunk results + picking best evidence "
              "per commitment...")
        aggregated = AggregatePerCommitment(canonical_commitments, all_chunk_results)
        status_summary = Counter(a["status"] for a in aggregated)
        print(f"  Code-based status counts: {dict(status_summary)}")

        final_rows: List[str] = []
        for entry in aggregated:
            if entry["candidates"]:
                evidence, page_y = PickBestEvidenceWithText(
                    client, entry["commitment"], args.year, entry["candidates"]
                )
            else:
                evidence, page_y = "無資料", "無資料"
            clue_cell = CluesToCell(entry.get("clues", []))
            final_rows.append(
                f"| {SanitizeCell(entry['commitment'])} | {entry['year_x']} | "
                f"{entry['year_y']} | {entry['status']} | {SanitizeCell(evidence)} | "
                f"{entry['page_x']} | {page_y} | {clue_cell} |"
            )

        final_md = (
            "| 承諾 | 發布年份 | 目標年份 | 達成狀態 | 證據 | 承諾來源頁碼 | "
            "證據來源頁碼 | 其他相關/可疑證據 |\n"
            "|---|---|---|---|---|---|---|---|\n"
            + "\n".join(final_rows)
        )

        print("\n--- Final verification table ---")
        print(final_md)

        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(final_md)

        print(f"\n[Success] Verification complete. Saved to: {out_path}")

    except Exception as error:
        # Includes OpenaiCallFailed (retries exhausted): a failed verify chunk
        # must not be written down as 未提及, so nothing is written and the exit
        # status says so; openai_run_company.py reports the year as failed.
        print(f"\n[Error] Pipeline execution failed: {error}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    Main()
