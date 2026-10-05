"""Run the full per-company promise pipeline: get -> consolidate -> check.

Usage:
    python openai_run_company.py --company <NAME>
                                 [--pdf_dir PDF_ROOT]
                                 [--image_dir IMAGE_ROOT]
                                 [--result_dir RESULT_ROOT]
                                 [--years 109 110 111 ...]
                                 [--skip_get]
                                 [--skip_consolidate]
                                 [--skip_check]

Discovers ROC years from {pdf_dir}/{company}/<year>.pdf (or takes them from
--years), sorts ascending, then runs:

  Phase 1: openai_get_promise.py        --year Y   for every Y (old -> new)
  Phase 2: openai_consolidate_promises.py          ONCE (all target years)
  Phase 3: openai_check_promise.py      --year Y   for every Y (old -> new)

Phase 1 must finish for ALL years before Phase 2, because consolidation folds
every year's promise.md together. Phase 2 must finish before Phase 3, because
check_promise reads the canonical promise/{T}.md files Phase 2 writes — this is
what gives a promise the SAME wording in every year that tracks it (so an early
fulfilment reads identically in the year it was delivered and in its target year).

Phase 3 order no longer affects correctness — each check_promise(Y) only reads
Phase 2's promise/{T}.md and the year-Y PDF, then writes that year's
check_result.md independently — but it is kept old -> new for deterministic,
readable output. The per-promise cross-year fold now lives entirely in
openai_build_summary_json.py, which reads every year's check_result.md after the
fact to build summary.json.

Phase 1 is resume-friendly: any year whose {result_dir}/{company}/{year}/
promise.md already exists is skipped, so re-running a company only extracts the
years still missing (delete a year's promise.md to force re-extraction).

Phase 3 is likewise resume-friendly: any year whose check_result.md already exists
is skipped (delete it to force re-verification).

A phase never runs on top of a failed upstream phase: if any get_promise year
failed, Phases 2 and 3 are blocked (and the exit status is non-zero); if
consolidation failed, Phase 3 is blocked. Otherwise an incomplete list would be
baked into promise/*.md and check_result.md, and a later successful re-run would
skip those files as "already done". (Files left by a run from before this rule
existed are not detected: delete the affected promise/*.md and check_result.md
by hand to have them rebuilt.)

Each invocation is a child subprocess. A non-zero exit in one year does not stop
the others; a per-phase summary is printed at the end, and the exit status of
this script is non-zero when any phase failed (or nothing could be run), so
openai_run_all.py and shell scripts can tell an incomplete company from a
finished one. If Phase 2 is skipped (or its promise/ output is missing),
check_promise falls back to collecting and consolidating each year's promise.md
on the fly.

--image_dir is unused by the PDF-file OpenAI flow but accepted for CLI parity and
forwarded to the child scripts.

Use --skip_get / --skip_consolidate / --skip_check to resume only some phases.
Use --years to restrict processing (e.g. to redo a specific year).
"""

import argparse
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GET_PROMISE = os.path.join(SCRIPT_DIR, "openai_get_promise.py")
CONSOLIDATE = os.path.join(SCRIPT_DIR, "openai_consolidate_promises.py")
CHECK_PROMISE = os.path.join(SCRIPT_DIR, "openai_check_promise.py")

_YEAR_PDF = re.compile(r"^(\d+)\.pdf$", re.IGNORECASE)


def DiscoverYears(pdf_dir: str, company: str) -> List[str]:
    """Scan {pdf_dir}/{company}/*.pdf and return ROC years as strings, ascending."""
    folder = os.path.join(pdf_dir, company)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Company PDF folder not found: {folder}")
    years: List[str] = []
    for entry in os.listdir(folder):
        m = _YEAR_PDF.match(entry)
        if m:
            years.append(m.group(1))
    return sorted(years, key=int)


def PromiseExists(result_dir: str, company: str, year: str) -> bool:
    """True iff {result_dir}/{company}/{year}/promise.md already exists (and is
    non-empty), i.e. get_promise has already produced this year's commitments."""
    path = os.path.join(result_dir, company, str(year), "promise.md")
    return os.path.isfile(path) and os.path.getsize(path) > 0


def CheckResultExists(result_dir: str, company: str, year: str) -> bool:
    """True iff {result_dir}/{company}/{year}/check_result.md already exists (and
    is non-empty), i.e. check_promise has already verified this year."""
    path = os.path.join(result_dir, company, str(year), "check_result.md")
    return os.path.isfile(path) and os.path.getsize(path) > 0


def RunScript(label: str, script: str, common: List[str],
              year: Optional[str] = None) -> int:
    """Invoke one of the pipeline scripts with the shared CLI args. With `year`
    the per-year `--year Y` flag is added; without it (e.g. consolidate, which
    runs once over all target years) only the common args are passed. Returns the
    child's exit code; stdout/stderr stream straight to this process."""
    cmd = [sys.executable, script]
    if year is not None:
        cmd += ["--year", year]
    cmd += common
    header = f"{label} year {year}" if year is not None else label
    print(f"\n=== {header} ===")
    print(f"  $ {' '.join(cmd)}")
    try:
        completed = subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print(f"  [interrupted] {header} aborted by user.")
        raise
    rc = completed.returncode
    print(f"  -> exit code {rc}")
    return rc


def _PrintPhaseSummary(label: str, results: Dict[str, int]) -> None:
    if not results:
        return
    ok = sum(1 for r in results.values() if r == 0)
    failed = sorted([y for y, r in results.items() if r != 0], key=int)
    line = f"  {label}: {ok}/{len(results)} OK"
    if failed:
        line += f"   failed years: {failed}"
    print(line)


def Main() -> int:
    p = argparse.ArgumentParser(
        description="Run get_promise -> consolidate_promises -> check_promise for "
                    "a company (OpenAI, PDF-file based)."
    )
    p.add_argument("--company", required=True,
                   help="Company folder under pdf_dir / image_dir / result_dir.")
    p.add_argument("--pdf_dir", default="pdf",
                   help="Root holding {company}/{year}.pdf (default: pdf).")
    p.add_argument("--image_dir", default="image",
                   help="Unused in PDF-file mode; forwarded for CLI parity.")
    p.add_argument("--result_dir", default="result",
                   help="Root for promise.md / check_result.md (default: result).")
    p.add_argument("--years", nargs="+", default=None,
                   help="Explicit ROC years to process (bypasses auto-discovery).")
    p.add_argument("--skip_get", action="store_true",
                   help="Skip Phase 1 (openai_get_promise).")
    p.add_argument("--skip_consolidate", action="store_true",
                   help="Skip Phase 2 (openai_consolidate_promises).")
    p.add_argument("--skip_check", action="store_true",
                   help="Skip Phase 3 (openai_check_promise).")
    args = p.parse_args()

    if args.years:
        try:
            years = sorted(set(args.years), key=int)
        except ValueError:
            print(f"[Error] --years must be integers, got: {args.years}", file=sys.stderr)
            sys.exit(2)
    else:
        years = DiscoverYears(args.pdf_dir, args.company)

    if not years:
        print(f"[Warn] No year PDFs found for {args.company} under {args.pdf_dir}/. "
              "Nothing to do.")
        return 1

    common = [
        "--company", args.company,
        "--pdf_dir", args.pdf_dir,
        "--image_dir", args.image_dir,
        "--result_dir", args.result_dir,
    ]

    print(f"=== openai_run_company  {args.company} ===")
    print(f"  years to process: {years}")
    print(f"  pdf_dir:    {args.pdf_dir}")
    print(f"  image_dir:  {args.image_dir}")
    print(f"  result_dir: {args.result_dir}")

    get_results: Dict[str, int] = {}
    check_results: Dict[str, int] = {}
    skipped_get: List[str] = []
    skipped_check: List[str] = []
    consolidate_rc: Optional[int] = None
    blocked: List[str] = []

    try:
        if not args.skip_get:
            print(f"\n##### Phase 1: get_promise ({len(years)} years, old -> new) #####")
            for year in years:
                # Resume-friendly: a year whose promise.md already exists is left
                # untouched so re-running the company only fills in missing years.
                if PromiseExists(args.result_dir, args.company, year):
                    print(f"\n=== get-promise year {year} ===")
                    print(f"  [Skip] {os.path.join(args.result_dir, args.company, year, 'promise.md')} "
                          f"already exists; skipping extraction.")
                    skipped_get.append(year)
                    continue
                get_results[year] = RunScript("get-promise", GET_PROMISE, common, year)
        else:
            print("\n[Skip] Phase 1 (get_promise) skipped via --skip_get.")

        # A phase never runs on top of a failed upstream phase: consolidating
        # without a failed year's promise.md would bake an incomplete list into
        # promise/*.md, and verifying against it would write check results that
        # silently lack commitments -- files a later, successful re-run would
        # then skip as "already done". Blocked phases count as failed.
        get_failed = sorted((y for y, rc in get_results.items() if rc != 0), key=int)

        # Phase 2 runs ONCE (not per-year): it folds every year's promise.md into
        # one canonical promise/{T}.md per target year so each promise keeps a
        # single stable wording across the years that later verify it. It must run
        # after ALL get_promise years and before any check_promise year.
        if args.skip_consolidate:
            print("\n[Skip] Phase 2 (consolidate_promises) skipped via --skip_consolidate.")
        elif get_failed:
            print(f"\n[Blocked] Phase 2 (consolidate_promises) not run: get_promise failed "
                  f"for years {get_failed}. Fix the cause and re-run.")
            blocked.append("consolidate")
        else:
            print("\n##### Phase 2: consolidate_promises (1 pass, all target years) #####")
            consolidate_rc = RunScript("consolidate-promises", CONSOLIDATE, common)

        if args.skip_check:
            print("\n[Skip] Phase 3 (check_promise) skipped via --skip_check.")
        elif get_failed or blocked or consolidate_rc not in (None, 0):
            print("\n[Blocked] Phase 3 (check_promise) not run: an upstream phase failed "
                  "(see above). Fix the cause and re-run.")
            blocked.append("check")
        else:
            print(f"\n##### Phase 3: check_promise ({len(years)} years, old -> new) #####")
            for year in years:
                # Resume-friendly: a year whose check_result.md already exists is
                # left untouched (mirrors the Phase 1 skip).
                if CheckResultExists(args.result_dir, args.company, year):
                    print(f"\n=== check-promise year {year} ===")
                    print(f"  [Skip] {os.path.join(args.result_dir, args.company, year, 'check_result.md')} "
                          f"already exists; skipping verification.")
                    skipped_check.append(year)
                    continue
                check_results[year] = RunScript("check-promise", CHECK_PROMISE, common, year)
    finally:
        print("\n=== Summary ===")
        _PrintPhaseSummary("get_promise  ", get_results)
        if skipped_get:
            print(f"  get_promise   skipped (promise.md already existed): {skipped_get}")
        if consolidate_rc is not None:
            status = "OK" if consolidate_rc == 0 else f"FAILED (exit {consolidate_rc})"
            print(f"  consolidate  : {status}")
        _PrintPhaseSummary("check_promise", check_results)
        if skipped_check:
            print(f"  check_promise skipped (check_result.md already existed): {skipped_check}")
        if blocked:
            print(f"  blocked (upstream phase failed): {blocked}")

    # A failed year leaves no output behind (or a stale one), so the company is
    # incomplete: say so in the exit status instead of letting a caller count
    # it as finished. Re-running resumes: finished years are skipped.
    failed = (any(rc != 0 for rc in get_results.values())
              or any(rc != 0 for rc in check_results.values())
              or consolidate_rc not in (None, 0)
              or bool(blocked))
    if failed:
        print("  RESULT: FAILED -- the outputs above are incomplete; fix the cause and re-run "
              "(finished years are skipped, failed ones are redone)")
        return 1
    print("  RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(Main())
