"""Load result/{company}/summary.json into the annotation DB, one item per commitment.

Each item carries a priority score so the dispatcher can serve the most likely
mis-graded commitments first. The flagship signal is exactly the pattern that
motivated this study: a year graded "achieved" followed by "partially
achieved" or "not mentioned".

    python ingest.py                       # ingest every company under RESULT_DIR
    python ingest.py --company ACME BETA   # only these
    python ingest.py --reset               # drop and rebuild the item table
    python ingest.py --dry_run --top 30    # print the priority ranking, write nothing
"""

import argparse
import json
import os
import re
import sys

import allocate
import common
import config

# Wording that signals the sentence is a plan, not an accomplishment (Chinese
# and English; the pipeline may keep the report language in the evidence).
_RESTATE_HINT = ("目標", "預計", "將於", "將持續", "規劃", "期望", "力求", "承諾於", "預期",
                 "will ", "plan", "aim", "target", "expect", "intend", "commit", "by 20")
_NUMBER = re.compile(r"\d")
# Clue text admitting a miss / a violation.
_NEGATIVE_HINT = ("未達成", "未達標", "違規", "罰鍰", "裁罰", "罰單", "缺失", "超標", "事故",
                  "not achieved", "not met", "violation", "penalt", "non-compliance",
                  "exceed", "incident", "accident")


def _Has(text, hints):
    low = text.lower()
    return any(h in low for h in hints)


def ScorePromise(rows, promise):
    """Return (priority, flags). Higher priority == more likely to be wrong."""
    flags = []
    score = 0.0
    seq = [r["ai_status"] for r in rows]
    A, N = config.AI_ACHIEVED, config.AI_NOT_MENTIONED

    # (1) achieved followed by anything else -- the pattern this study is about.
    first_a = next((i for i, s in enumerate(seq) if s == A), None)
    if first_a is not None:
        regress = [s for s in seq[first_a + 1 :] if s != A]
        if regress:
            score += 50 + 5 * len(regress)
            flags.append(f"achieved_then_regress:{len(regress)}")

    # (2) A P A P oscillation.
    transitions = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    if transitions >= 3:
        score += 10 * (transitions - 2)
        flags.append(f"oscillation:{transitions}")

    # (3) Same evidence sentence, different verdicts across years.
    by_evidence = {}
    for r in rows:
        if len(r["evidence"]) >= 12:
            by_evidence.setdefault(r["evidence"], set()).add(r["ai_status"])
    if any(len(v) > 1 for v in by_evidence.values()):
        score += 25
        flags.append("duplicate_evidence_diff_status")

    # (4) achieved backed by a sentence that only restates the goal.
    restated = [
        r["year"]
        for r in rows
        if r["ai_status"] == A
        and r["evidence"]
        and _Has(r["evidence"], _RESTATE_HINT)
        and not _NUMBER.search(r["evidence"])
    ]
    if restated:
        score += 3 * len(restated)
        flags.append("restatement_as_evidence:" + ",".join(map(str, restated)))

    # (5) Verdict says achieved but its own clues admit a miss.
    conflict = [
        r["year"]
        for r in rows
        if r["ai_status"] == A and any(_Has(c, _NEGATIVE_HINT) for c in r["clues"])
    ]
    if conflict:
        score += 6
        flags.append("evidence_vs_clue:" + ",".join(map(str, conflict)))

    # (6) achieved with no evidence text at all.
    empty = [r["year"] for r in rows if r["ai_status"] == A and not r["evidence"]]
    if empty:
        score += 20
        flags.append("achieved_without_evidence:" + ",".join(map(str, empty)))

    # (7) Overall conclusion disagrees with the last observed year's status.
    fs = promise.get("final_status")
    if fs and seq and fs != seq[-1]:
        score += 20
        flags.append(f"final_last_year_mismatch:{fs}!={seq[-1]}")

    # (8) Went silent: a "not mentioned" year right after a year that had
    # something. Retrieval misses and quietly-disappearing targets both look
    # like this.
    silent = [rows[i]["year"] for i in range(1, len(seq))
              if seq[i] == N and seq[i - 1] != N]
    if silent:
        score += 12 * len(silent)
        flags.append("went_silent:" + ",".join(map(str, silent)))

    # (9) Year parsing bug: a target year earlier than the declaration year is
    # impossible. Out-of-range years are not penalised here -- the annotators'
    # window check catches those.
    dy, ty = promise.get("declared_year"), promise.get("target_year")
    if isinstance(ty, int) and isinstance(dy, int) and ty < dy:
        score += 40
        flags.append(f"bad_target_year:{ty}")

    # (10) Long traces are simply worth more annotator attention.
    score += 1.5 * len(rows)
    return score, flags


def IngestCompany(conn, result_dir, company, industry_map, dry_run=False):
    path = os.path.join(result_dir, company, "summary.json")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        summary = json.load(f)

    made = []
    for promise in summary.get("promises", []):
        commitment = (promise.get("commitment") or "").strip()
        if not commitment:
            continue
        rows = common.YearRows(promise)
        if not rows:
            continue
        priority, flags = ScorePromise(rows, promise)
        # Two strata: flagged commitments feed the error hunt, unflagged ones
        # are sampled uniformly so the dataset keeps a realistic base rate.
        record = {
            "stratum": "anomaly" if flags else "control",
            "company": company,
            "industry": industry_map.get(company, "n/a"),
            "commitment": commitment,
            "declared_year": promise.get("declared_year"),
            "target_year": promise.get("target_year"),
            "source_page": str(promise.get("source_page") or ""),
            "final_status": promise.get("final_status"),
            "n_years": len(rows),
            "seq": common.SeqString(rows),
            "letters": common.LetterString(rows),
            "payload": json.dumps(promise, ensure_ascii=False),
            "priority": priority,
            "flags": json.dumps(flags, ensure_ascii=False),
        }
        made.append(record)
        if dry_run:
            continue
        conn.execute(
            """INSERT INTO item (company, industry, commitment, declared_year,
                                 target_year, source_page, final_status, n_years,
                                 seq, letters, payload, priority, flags, stratum)
               VALUES (:company, :industry, :commitment, :declared_year,
                       :target_year, :source_page, :final_status, :n_years,
                       :seq, :letters, :payload, :priority, :flags, :stratum)
               ON CONFLICT (company, commitment, declared_year, target_year)
               DO UPDATE SET payload = excluded.payload,
                             final_status = excluded.final_status,
                             seq = excluded.seq,
                             letters = excluded.letters,
                             n_years = excluded.n_years,
                             priority = excluded.priority,
                             flags = excluded.flags,
                             stratum = excluded.stratum""",
            record,
        )
    return made


def MarkCalibration(conn, n):
    """Make the n highest-scoring items (ScorePromise's priority -- the
    anomaly/penalty score) the shared calibration set: served first to
    everyone, in that order, exempt from the redundancy cap and from the
    balanced allocation. Straight top of the ranking, with item_id breaking
    ties, so re-running picks exactly the same set.

    No further filter: the worst-looking traces are where annotators' standards
    differ most, which is the whole point of a shared item. (An item whose
    target year is still in the future can be calibrated on just as well -- the
    annotators are judging the AI's yearly verdicts, not the final outcome.)
    Use ESG_GOLD_FILE when a hand-picked set is wanted instead."""
    conn.execute("UPDATE item SET is_gold = 0, gold_rank = NULL, gold_expect = NULL")
    chosen = [r["item_id"] for r in conn.execute(
        "SELECT item_id FROM item WHERE active = 1 ORDER BY priority DESC, item_id LIMIT ?",
        (max(0, n),))]
    for rank, item_id in enumerate(chosen, start=1):
        conn.execute("UPDATE item SET is_gold = 1, gold_rank = ?, gold_expect = '' "
                     "WHERE item_id = ?", (rank, item_id))
    return len(chosen)


def LoadGold(conn, gold_file):
    """Curated alternative to MarkCalibration. File format: list of
    {"company":..,"declared_year":..,"commitment":..,
     "expect":{"is_commitment":"valid","year_status":{"112":"Not yet achieved"}}}
    List order is the order every annotator sees them in; "expect" is optional
    (without it the item calibrates but is not pass/fail scored).
    """
    with open(gold_file, "r", encoding="utf-8") as f:
        golds = json.load(f)
    conn.execute("UPDATE item SET is_gold = 0, gold_rank = NULL, gold_expect = NULL")
    hit = 0
    for rank, g in enumerate(golds, start=1):
        cur = conn.execute(
            "UPDATE item SET is_gold = 1, gold_rank = ?, gold_expect = ? "
            "WHERE company = ? AND commitment = ? AND declared_year = ?",
            (rank, json.dumps(g["expect"], ensure_ascii=False) if g.get("expect") else "",
             g["company"], g["commitment"], g["declared_year"]),
        )
        if cur.rowcount:
            hit += 1
        else:
            print(f"  ! gold not matched: [{g['company']}] {g['commitment']!r}", file=sys.stderr)
    return hit


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--result_dir", default=config.RESULT_DIR)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--company", nargs="*", help="limit to these companies")
    ap.add_argument("--gold_file", default=config.GOLD_FILE or None,
                    help="json list of curated gold items (default: ESG_GOLD_FILE)")
    ap.add_argument("--reset", action="store_true", help="wipe items (and all annotations!)")
    ap.add_argument("--dry_run", action="store_true", help="score only, write nothing")
    ap.add_argument("--top", type=int, default=0, help="print the top-N priority ranking")
    args = ap.parse_args()

    if not os.path.isdir(args.result_dir):
        raise SystemExit(f"result dir not found: {args.result_dir}\n"
                         "expected <result_dir>/<company>/summary.json -- set ESG_RESULT_DIR "
                         "or pass --result_dir")

    conn = common.InitDb(args.db)
    if args.reset and not args.dry_run:
        confirm = input("This deletes every annotation collected so far. Type YES: ")
        if confirm != "YES":
            raise SystemExit("aborted")
        # Child tables first (draft and mark reference assignment, alloc
        # references item), and all of it in one transaction: a failure
        # part-way must leave the DB exactly as it was, never half-wiped.
        conn.execute("BEGIN")
        try:
            for t in ("verdict", "final", "ante", "draft", "mark", "assignment", "alloc", "item"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if args.company:
        companies = args.company
    else:
        companies = sorted(
            d for d in os.listdir(args.result_dir)
            if not d.startswith("_")
            and os.path.isfile(os.path.join(args.result_dir, d, "summary.json"))
        )
    if not companies:
        raise SystemExit(f"no <company>/summary.json under {args.result_dir}")

    everything = []
    conn.execute("BEGIN")
    industry_map = common.LoadIndustryMap()
    for company in companies:
        made = IngestCompany(conn, args.result_dir, company, industry_map, args.dry_run)
        everything.extend(made)
        print(f"{company:>20}: {len(made)} commitments")
    if not args.dry_run:
        if args.gold_file:
            print(f"calibration items (curated list): {LoadGold(conn, args.gold_file)}")
        else:
            print(f"calibration items (top {config.CALIBRATION_N} by anomaly score): "
                  f"{MarkCalibration(conn, config.CALIBRATION_N)}")
        allocate.Allocate(conn, config.N_ANNOTATORS, config.REDUNDANCY, config.REDUNDANCY_CONTROL)
    conn.execute("COMMIT")
    if not args.dry_run:
        allocate.PrintSummary(conn)

    flagged = [r for r in everything if r["stratum"] == "anomaly"]
    regress = [r for r in everything
               if any(f.startswith("achieved_then_regress") for f in json.loads(r["flags"]))]
    print(f"\ntotal {len(everything)} items: {len(flagged)} flagged / "
          f"{len(everything) - len(flagged)} control, "
          f"{len(regress)} with an achieved -> not-achieved regression")
    if len(everything) - len(flagged) == 0:
        print("  ! no control items -- every commitment is flagged, so the sample "
              "will be all anomalies. Loosen the scoring rules in ScorePromise.")

    if args.top:
        print(f"\ntop {args.top} by priority:")
        for r in sorted(everything, key=lambda x: -x["priority"])[: args.top]:
            print(f"  {r['priority']:6.1f}  [{r['company']}] {r['letters']}  "
                  f"{r['commitment'][:38]}  {json.loads(r['flags'])}")


if __name__ == "__main__":
    main()
