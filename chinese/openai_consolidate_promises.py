"""Consolidate a company's per-year promise.md files into one canonical list per
target year (OpenAI, judge-LLM based).

Usage:
    python openai_consolidate_promises.py --company <NAME>
                                          [--pdf_dir PDF_ROOT]
                                          [--image_dir IMAGE_ROOT]
                                          [--result_dir RESULT_ROOT]
                                          [--years 113 114 115 ...]

Every {result_dir}/{company}/{X}/promise.md (written by openai_get_promise.py)
lists the future-year commitments declared in report year X. The SAME future
promise is usually re-stated in several years' reports, each time with slightly
different wording. If each check_promise(Y) run consolidated those independently,
the canonical wording would drift from year to year.

This script fixes the wording ONCE per target year. It groups every promise.md
row by its 目標年份 T, and for each T runs a single judge consolidation pass
(DedupSingleTargetPromisesWithText) that merges semantic duplicates and emits one
stable, reusable description per promise. The result is written to
{result_dir}/{company}/promise/{T}.md.

openai_check_promise.py then reads these promise/{T}.md files (for every T >= the
year being checked) via LoadCanonicalPromisesForYear, so a promise fulfilled
early reads identically in the year it was delivered and in its target year.

This is a markdown -> judge-LLM -> markdown transform; no PDF reads. It is
resume-friendly: a target year whose promise/{T}.md already exists is skipped, so
re-running only (re)builds the missing target years — delete a target's file to
force its re-consolidation. With --years, only those target years are considered.
--pdf_dir / --image_dir are unused here but accepted for CLI parity with the
other pipeline scripts.
"""

import argparse
import os
import sys
import traceback
from typing import Dict, List

from openai_utils import (
    CommitmentsToMarkdown,
    CreateOpenaiClient,
    DedupSingleTargetPromisesWithText,
    JUDGE_MODEL,
    ParseExtractionTable,
    _ParseRocYear,
)


def CollectPromisesByTarget(
    result_dir: str, company: str,
) -> Dict[int, Dict[str, List[Dict[str, str]]]]:
    """Scan {result_dir}/{company}/{X}/promise.md for every numeric year X and
    bucket the rows by their 目標年份 T.

    Returns {T: {X_label: [row, ...]}} — i.e. for each target year T, the rows
    declaring that promise grouped by the report year X that declared them.
    """
    base = os.path.join(result_dir, company)
    by_target: Dict[int, Dict[str, List[Dict[str, str]]]] = {}
    if not os.path.isdir(base):
        return by_target

    for entry in sorted(os.listdir(base)):
        try:
            int(entry)
        except ValueError:
            continue  # skip the promise/ dir and any non-year folders
        promise_path = os.path.join(base, entry, "promise.md")
        if not os.path.isfile(promise_path):
            continue
        with open(promise_path, "r", encoding="utf-8") as f:
            rows = ParseExtractionTable(f.read())
        for row in rows:
            t = _ParseRocYear(row.get("year_y", ""))
            if t is None:
                continue
            by_target.setdefault(t, {}).setdefault(entry, []).append(row)
    return by_target


def Main():
    parser = argparse.ArgumentParser(
        description="Consolidate per-year promise.md files into one canonical "
                    "list per target year (OpenAI, judge-LLM based)."
    )
    parser.add_argument("--company", required=True,
                        help="Company folder under result_dir.")
    parser.add_argument("--pdf_dir", default="pdf",
                        help="Unused here; kept for CLI parity.")
    parser.add_argument("--image_dir", default="image",
                        help="Unused here; kept for CLI parity.")
    parser.add_argument("--result_dir", default="result",
                        help="Root holding {company}/{X}/promise.md (default: result).")
    parser.add_argument("--years", nargs="+", default=None,
                        help="Explicit target ROC years to (re)build (default: all).")
    args = parser.parse_args()

    by_target = CollectPromisesByTarget(args.result_dir, args.company)
    if not by_target:
        print(f"[Warn] No {args.result_dir}/{args.company}/<year>/promise.md files "
              f"found. Run openai_get_promise.py first. Nothing to do.")
        return

    targets = sorted(by_target.keys())
    if args.years:
        try:
            wanted = {int(y) for y in args.years}
        except ValueError:
            print(f"[Error] --years must be integers, got: {args.years}")
            sys.exit(2)
        targets = [t for t in targets if t in wanted]
        if not targets:
            print(f"[Warn] None of --years {sorted(wanted)} have promises "
                  f"(available targets: {sorted(by_target.keys())}). Nothing to do.")
            return

    out_dir = os.path.join(args.result_dir, args.company, "promise")
    os.makedirs(out_dir, exist_ok=True)

    # Resume-friendly: a target year whose canonical promise/{T}.md already exists
    # is left untouched, so re-running only (re)builds the missing target years
    # (delete a target's file to force its re-consolidation).
    pending: List[int] = []
    skipped: List[int] = []
    for t in targets:
        out_path = os.path.join(out_dir, f"{t}.md")
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            skipped.append(t)
        else:
            pending.append(t)

    print(f"=== openai_consolidate_promises  {args.company} ===")
    print(f"  result_dir:     {args.result_dir}")
    print(f"  judge model:    {JUDGE_MODEL}")
    print(f"  target years:   {targets}")
    if skipped:
        print(f"  skip (exists):  {skipped}")
    print(f"  to build:       {pending}")
    print(f"  output dir:     {out_dir}")

    if not pending:
        print("\n[Skip] All target-year canonical files already exist; nothing to do.")
        return

    client = CreateOpenaiClient()
    written: List[int] = []
    failed: List[int] = []

    for t in pending:
        per_source = {
            x_label: CommitmentsToMarkdown(rows)
            for x_label, rows in sorted(by_target[t].items(), key=lambda kv: int(kv[0]))
        }
        n_sources = len(per_source)
        n_rows = sum(len(rows) for rows in by_target[t].values())
        print(f"\n--- target year {t}: {n_rows} row(s) from {n_sources} report(s) "
              f"{sorted(by_target[t].keys(), key=int)} ---")

        try:
            canonical_md = DedupSingleTargetPromisesWithText(
                client, per_source, str(t)
            )
        except Exception as error:
            # Includes OpenaiCallFailed (retries exhausted): this target year is
            # left unwritten and reported in the exit status below.
            print(f"  [Error] consolidation failed for target {t}: {error}")
            traceback.print_exc()
            failed.append(t)
            continue

        canonical_rows = ParseExtractionTable(canonical_md)
        if not canonical_rows:
            print(f"  [Error] target {t}: consolidation produced no rows; nothing written.")
            failed.append(t)
            continue

        out_path = os.path.join(out_dir, f"{t}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(CommitmentsToMarkdown(canonical_rows))
        written.append(t)
        print(f"  [Success] {len(canonical_rows)} canonical promise(s) -> {out_path}")

    print(f"\n=== Done: wrote {len(written)}/{len(pending)} target-year files "
          f"{written} ===")
    if skipped:
        print(f"  skipped (already existed): {skipped}")
    if failed:
        # Non-zero so openai_run_company.py reports Phase 2 as failed; the
        # written target years are skipped on the next run, the failed ones
        # are retried.
        print(f"  FAILED target years: {failed} -- re-run to retry them")
        sys.exit(1)


if __name__ == "__main__":
    Main()
