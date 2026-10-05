"""Run openai_run_company.py for every company under pdf/.

Usage:
    python openai_run_all.py [--pdf_dir pdf]
                             [--image_dir image]
                             [--result_dir result]
                             [--companies A B C ...]
                             [--skip_get] [--skip_consolidate] [--skip_check]

Enumerates each immediate subdirectory of --pdf_dir as a company and invokes
openai_run_company.py for it (which itself discovers that company's ROC years and
runs get -> consolidate -> check). Companies are processed in sorted order; a
non-zero exit for one company does NOT stop the rest, and a final OK/failed
summary is printed. The exit status of this script is non-zero when any company
failed or the run was interrupted, so a wrapper cannot mistake a partial run for
a complete one.

Because the underlying pipeline is resume-friendly (years with an existing
promise.md skip extraction; target years with an existing promise/{T}.md skip
consolidation; years with an existing check_result.md skip verification),
re-running this script only fills in the work still missing.

--companies restricts the run to an explicit subset (still resolved under
--pdf_dir). The skip flags are forwarded unchanged to every child invocation.
--years is intentionally NOT exposed here because each company has its own years.
"""

import argparse
import os
import subprocess
import sys
from typing import Dict, List


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_COMPANY = os.path.join(SCRIPT_DIR, "openai_run_company.py")


def DiscoverCompanies(pdf_dir: str) -> List[str]:
    """Immediate subdirectories of pdf_dir, sorted — one per company."""
    if not os.path.isdir(pdf_dir):
        raise FileNotFoundError(f"PDF root not found: {pdf_dir}")
    return sorted(
        name for name in os.listdir(pdf_dir)
        if os.path.isdir(os.path.join(pdf_dir, name))
    )


def Main() -> int:
    p = argparse.ArgumentParser(
        description="Run openai_run_company.py for every company under pdf/."
    )
    p.add_argument("--pdf_dir", default="pdf",
                   help="Root holding {company}/{year}.pdf (default: pdf).")
    p.add_argument("--image_dir", default="image",
                   help="Unused in PDF-file mode; forwarded for CLI parity.")
    p.add_argument("--result_dir", default="result",
                   help="Root for promise.md / check_result.md (default: result).")
    p.add_argument("--companies", nargs="+", default=None,
                   help="Explicit company subset (default: all folders under pdf_dir).")
    p.add_argument("--skip_get", action="store_true",
                   help="Forward --skip_get to each company run.")
    p.add_argument("--skip_consolidate", action="store_true",
                   help="Forward --skip_consolidate to each company run.")
    p.add_argument("--skip_check", action="store_true",
                   help="Forward --skip_check to each company run.")
    args = p.parse_args()

    companies = args.companies or DiscoverCompanies(args.pdf_dir)
    if not companies:
        print(f"[Warn] No company folders found under {args.pdf_dir}/. Nothing to do.")
        return 1

    forwarded = [
        "--pdf_dir", args.pdf_dir,
        "--image_dir", args.image_dir,
        "--result_dir", args.result_dir,
    ]
    if args.skip_get:
        forwarded.append("--skip_get")
    if args.skip_consolidate:
        forwarded.append("--skip_consolidate")
    if args.skip_check:
        forwarded.append("--skip_check")

    print(f"=== openai_run_all  ({len(companies)} companies) ===")
    print(f"  pdf_dir:    {args.pdf_dir}")
    print(f"  result_dir: {args.result_dir}")
    print(f"  companies:  {companies}")

    results: Dict[str, int] = {}
    interrupted = False
    try:
        for company in companies:
            cmd = [sys.executable, RUN_COMPANY, "--company", company] + forwarded
            print(f"\n##### company {company} #####")
            print(f"  $ {' '.join(cmd)}")
            completed = subprocess.run(cmd, check=False)
            results[company] = completed.returncode
            print(f"  -> {company} exit code {completed.returncode}")
    except KeyboardInterrupt:
        print("\n[Interrupted] aborted by user.")
        interrupted = True
    finally:
        print("\n=== Summary (per company) ===")
        ok = sum(1 for rc in results.values() if rc == 0)
        print(f"  {ok}/{len(results)} companies OK")
        failed = sorted(c for c, rc in results.items() if rc != 0)
        if failed:
            print(f"  failed: {failed}")
        not_run = [c for c in companies if c not in results]
        if not_run:
            print(f"  not run (interrupted): {not_run}")

    # The run is complete only if every company finished cleanly.
    if failed or not_run or interrupted:
        print("  RESULT: FAILED -- re-run to resume (finished work is skipped)")
        return 1
    print("  RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(Main())
