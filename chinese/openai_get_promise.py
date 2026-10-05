"""Extract every future-year-targeted commitment from one year's ESG report
(OpenAI, PDF-file based).

Usage:
    python openai_get_promise.py --company <NAME> --year <ROC_YEAR>
                                 [--pdf_dir PDF_ROOT]
                                 [--image_dir IMAGE_ROOT]
                                 [--result_dir RESULT_ROOT]
                                 [--report_path PATH]

Reads {pdf_dir}/{company}/{year}.pdf (or --report_path) and sends it to the
vision model as overlapping PDF chunks (20 pages, 2-page overlap: 1-20, 19-38,
...). Each recorded commitment must target a future year (strictly after the
report year, i.e. >= year + 1). After extraction it runs ONE judge consolidation
pass to merge semantically similar / duplicate commitments, then writes the
canonical 4-column table to {result_dir}/{company}/{year}/promise.md.

Downstream `openai_check_promise.py` later picks the rows where 目標年份 matches
the year being checked.

--image_dir is accepted for CLI parity with the image-based pipelines but is
unused in this PDF-file flow.
"""

import argparse
import os
import sys
import traceback
from typing import Dict, List

from openai_utils import (
    CHUNK_OVERLAP,
    CommitmentsToMarkdown,
    CreateOpenaiClient,
    DedupAllPromisesWithText,
    ExtractModelPromises,
    IsFutureTargetYear,
    JUDGE_MODEL,
    PAGES_PER_CHUNK,
    ParseExtractionTable,
    VISION_MODELS,
)


def Main():
    parser = argparse.ArgumentParser(
        description="Extract every future-year-targeted commitment from one "
                    "year's ESG report (OpenAI, PDF-file based)."
    )
    parser.add_argument("--company", required=True,
                        help="Company folder under pdf_dir / result_dir.")
    parser.add_argument("--year", required=True,
                        help="Report year (民國年, e.g. 110).")
    parser.add_argument("--pdf_dir", default="pdf",
                        help="Root holding {company}/{year}.pdf (default: pdf).")
    parser.add_argument("--image_dir", default="image",
                        help="Unused in PDF-file mode; kept for CLI parity.")
    parser.add_argument("--result_dir", default="result",
                        help="Root for output promise.md (default: result).")
    parser.add_argument("--report_path", default=None,
                        help="Explicit PDF path override; bypasses pdf_dir/company/year.")
    args = parser.parse_args()

    pdf_path = args.report_path or os.path.join(
        args.pdf_dir, args.company, f"{args.year}.pdf")
    out_path = os.path.join(
        args.result_dir, args.company, str(args.year), "promise.md")

    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"Report PDF not found: {pdf_path}")

    client = CreateOpenaiClient()

    try:
        print(f"\n=== get-promise ({args.company} {args.year}) ===")
        print(f"  pdf:           {pdf_path}")
        print(f"  output:        {out_path}")
        print(f"  judge model:   {JUDGE_MODEL}")
        print(f"  vision models: {VISION_MODELS}")
        print(f"  chunk:         {PAGES_PER_CHUNK} pages, {CHUNK_OVERLAP}-page overlap")

        # ----- Step 1: per-model extraction from PDF chunks -----
        print(f"\n[Step 1] Per-model promise extraction from year {args.year} report...")
        per_model_rows: Dict[str, List[Dict[str, str]]] = {}
        for model_name in VISION_MODELS:
            per_model_rows[model_name] = ExtractModelPromises(
                client, model_name, pdf_path, args.year
            )
            print(f"  [{model_name}] -> {len(per_model_rows[model_name])} unique promises")

        per_model_md = {
            m: CommitmentsToMarkdown(rows)
            for m, rows in per_model_rows.items() if rows
        }
        if not per_model_md:
            print("\n[Warn] No promises extracted by any model. Stopping.")
            return

        # ----- Step 2: consolidate (judge dedup of similar/duplicate items) -----
        print(f"\n[Step 2] Consolidation pass with judge model ({JUDGE_MODEL}) to "
              f"merge semantically similar / duplicate commitments...")
        for model_name, table_md in per_model_md.items():
            print(f"--- [{model_name}] extracted promises ---")
            print(table_md)

        canonical_md = DedupAllPromisesWithText(client, per_model_md, args.year)
        print("\n--- Canonical promise table (post-consolidation) ---")
        print(canonical_md)

        # ----- Step 3: enforce future target-year (must be > report year) -----
        canonical_rows = ParseExtractionTable(canonical_md)
        future_rows = [
            r for r in canonical_rows
            if IsFutureTargetYear(r.get("year_y", ""), args.year)
        ]
        dropped = len(canonical_rows) - len(future_rows)
        if dropped:
            print(f"\n[Step 3] Dropped {dropped} commitment(s) whose 目標年份 is not "
                  f"in the future (must be > report year {args.year}).")
        if not future_rows:
            print("\n[Warn] No commitments with a future target year remain. Stopping.")
            return

        canonical_md = CommitmentsToMarkdown(future_rows)
        print(f"\n--- Final promise table ({len(future_rows)} future-target "
              f"commitments) ---")
        print(canonical_md)

        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(canonical_md)

        print(f"\n[Success] Promise extraction complete. Saved to: {out_path}")

    except Exception as error:
        # Includes OpenaiCallFailed (retries exhausted): nothing is written and
        # the exit status says so, so openai_run_company.py reports the year as
        # failed rather than finished.
        print(f"\n[Error] Pipeline execution failed: {error}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    Main()
