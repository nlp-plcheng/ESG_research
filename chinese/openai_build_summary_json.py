"""Consolidate a company's per-year check_result.md verdicts into one summary.json.

Usage:
    python openai_build_summary_json.py [--company NAME] [--result_dir result]

Reads every {result_dir}/{company}/{year}/check_result.md and writes ONE JSON
file per company at {result_dir}/{company}/summary.json.

Each promise is tracked from its declaration year through its target year as a
single entry whose "status" field is a YEAR-BY-YEAR list: one record per report
year that verified the promise, holding that year's 達成狀態 plus the evidence,
evidence page and related clues found in that year's report. A folded
"final_status" (已達成 > 部分達成 > 未提及 across the whole timeline) is kept for
convenience, and the company-level status_counts count promises by final_status.

With no --company, every company folder under result_dir is processed. This is a
pure markdown -> JSON transform; no LLM calls and no PDF reads.
"""

import argparse
import json
import os
from typing import Dict, List, Optional

from openai_utils import BuildCompanyPromiseTimelines


def _FinalStatus(timeline: List[Dict]) -> str:
    """Fold a per-year timeline into one verdict: any 已達成 -> 已達成; else any
    部分達成 -> 部分達成; else 未提及."""
    statuses = [e.get("status") for e in timeline]
    if "已達成" in statuses:
        return "已達成"
    if "部分達成" in statuses:
        return "部分達成"
    return "未提及"


def _ToEntry(promise: Dict) -> Dict:
    """Shape one BuildCompanyPromiseTimelines record into a summary.json promise:
    the per-year list is exposed as "status"; a folded "final_status" is added."""
    timeline = promise.get("timeline", [])
    return {
        "commitment": promise.get("commitment", ""),
        "declared_year": promise.get("declared_year"),
        "target_year": promise.get("target_year"),
        "source_page": promise.get("source_page"),
        "final_status": _FinalStatus(timeline),
        "status": timeline,
    }


def BuildCompanySummary(result_dir: str, company: str) -> Optional[Dict]:
    """Build the summary.json object for one company, or None when the company
    folder is missing."""
    base = os.path.join(result_dir, company)
    if not os.path.isdir(base):
        return None

    entries = [_ToEntry(p) for p in BuildCompanyPromiseTimelines(result_dir, company)]
    entries.sort(key=lambda e: (e["target_year"] or 0,
                                e["declared_year"] or 0,
                                e["commitment"]))

    status_counts: Dict[str, int] = {}
    for e in entries:
        status_counts[e["final_status"]] = status_counts.get(e["final_status"], 0) + 1

    return {
        "company": company,
        "total_promises": len(entries),
        "status_counts": status_counts,
        "promises": entries,
    }


def _DiscoverCompanies(result_dir: str) -> List[str]:
    """Company folders under result_dir that hold at least one
    {year}/check_result.md."""
    if not os.path.isdir(result_dir):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(result_dir)):
        cdir = os.path.join(result_dir, name)
        if not os.path.isdir(cdir):
            continue
        for sub in os.listdir(cdir):
            if os.path.isfile(os.path.join(cdir, sub, "check_result.md")):
                out.append(name)
                break
    return out


def Main():
    parser = argparse.ArgumentParser(
        description="Consolidate per-year check_result.md verdicts into summary.json."
    )
    parser.add_argument("--company", default=None,
                        help="Company folder under result_dir (default: all).")
    parser.add_argument("--result_dir", default="result",
                        help="Root holding {company}/{year}/check_result.md (default: result).")
    args = parser.parse_args()

    companies = [args.company] if args.company else _DiscoverCompanies(args.result_dir)
    if not companies:
        print(f"[Warn] No companies with check_result.md found under {args.result_dir}/.")
        return

    for company in companies:
        result = BuildCompanySummary(args.result_dir, company)
        if result is None:
            print(f"[Warn] {company}: result folder missing; skipped.")
            continue
        out_path = os.path.join(args.result_dir, company, "summary.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[Success] {company}: {result['total_promises']} promises "
              f"({result['status_counts']}) -> {out_path}")


if __name__ == "__main__":
    Main()
