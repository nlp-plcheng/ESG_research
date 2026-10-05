"""Merge the AI's original verdicts with the human correction records into CSVs.

    python export.py --out_dir ./exports

Produces three files:

  esg_item.csv  one row per (item, annotator): the AI final_status next to the
                human conclusion, and the flags that got the item prioritised.
  esg_year.csv  one row per (item, year, annotator): AI status, human status,
                evidence quality, and the forward-looking 0-100 estimate of
                the target-year record. This is the correction record proper
                and the risk-model training file.
  esg_risk.csv  one row per item, aggregated by majority vote: features + the
                per-year probability trajectory + the human-verified outcome.
"""

import argparse
import csv
import io
import json
import os
from collections import Counter

import common
import config

# Ordinal for tie-breaking majority votes on the overall conclusion (three
# labels; the older names still appear in early rows).
STATUS_RANK = {"未提及": 0, "未達成": 1, "遠離目標": 1, "部分達成": 1, "尚未達成": 1, "已達成": 2}
PROB_RANK = {"low": 0, "mid": 1, "high": 2}


def _Writer(header):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    return buf, w


def _PageShift(payload):
    """The page_shift.py record of this item: how each cited page was moved
    onto PDF page indices ({'at', 'src': {orig, d, kind}, 'years': {year:
    {'evidence': {...}, 'clues': [...]}}}); {} when never shifted."""
    try:
        return json.loads(payload or "{}").get("page_shift") or {}
    except ValueError:
        return {}


def _PageCols(rec, current, shift, answered_at, seen_db=None):
    """(orig, seen, offset, kind) for one cited page. `seen` is the number the
    annotator had in front of them: recorded with the answer (seen_db, from
    the page's hidden field) whenever the platform version that wrote the
    answer had it; for older answers, the original when the answer predates
    the shift, the current one otherwise."""
    rec = rec or {}
    orig = rec.get("orig", "")
    if seen_db is not None:
        seen = seen_db
    else:
        at = shift.get("at")
        seen = orig if (at and (answered_at or "") < at and rec) else current
    return orig if orig is not None else "", seen if seen is not None else "", \
        rec.get("d", 0), rec.get("kind", "")


def ItemCsv(conn):
    buf, w = _Writer([
        "item_id", "company", "industry", "commitment", "declared_year", "target_year",
        "source_page", "source_page_orig", "source_page_seen", "source_page_offset",
        "source_page_kind", "source_page_ok", "source_page_fix",
        "ai_final_status", "ai_seq", "ai_letters", "priority", "flags",
        "stratum", "is_gold", "prolific_pid", "is_commitment", "not_reason",
        "commitment_fix", "target_year_fix", "human_final_status", "ai_final_agree",
        "window_ok", "correct_target_year", "n_year_flips", "comment",
        "n_page_views", "seconds", "active_seconds", "submitted_at", "n_edits", "edited_at",
        "starred", "note",
    ])
    rows = conn.execute(
        """SELECT i.*, a.assign_id, a.submitted_at, a.n_edits, a.edited_at, wk.prolific_pid,
                  COALESCE(m.starred, 0) AS starred, COALESCE(m.note, '') AS note,
                  an.is_commitment, an.not_reason, an.commitment_fix, an.target_year_fix,
                  an.active_seconds, an.source_page_ok, an.source_page_fix, an.seen_source_page,
                  f.human_final_status, f.ai_final_agree, f.comment,
                  f.seconds AS task_seconds,
                  f.window_ok, f.correct_target_year, f.n_page_views,
                  (SELECT COUNT(*) FROM verdict v
                    WHERE v.assign_id = a.assign_id AND v.ai_status <> v.human_status) AS flips
             FROM assignment a
             JOIN item i   ON i.item_id = a.item_id
             JOIN worker wk ON wk.worker_id = a.worker_id
             LEFT JOIN ante an ON an.assign_id = a.assign_id
             LEFT JOIN final f ON f.assign_id = a.assign_id
             LEFT JOIN mark m ON m.assign_id = a.assign_id
            WHERE a.stage = 3 AND wk.is_test = 0
            ORDER BY i.priority DESC, i.item_id, a.assign_id"""
    ).fetchall()
    for r in rows:
        # Both conclusions in the current three labels; agreement is derived
        # here from the mapped labels (rows written under the earlier label
        # names carry a stale stored ai_final_agree).
        ai_final = common.FinalStatus(r["final_status"])
        human_final = common.FinalStatus(r["human_final_status"])
        agree = int(human_final == ai_final) if human_final else ""
        shift = _PageShift(r["payload"])
        w.writerow([
            r["item_id"], r["company"], r["industry"], r["commitment"], r["declared_year"],
            r["target_year"], r["source_page"],
            # As the pipeline wrote it / as this annotator saw it / how it was
            # moved onto the PDF page index (see page_shift.py, DATASET_SCHEMA §2b).
            *_PageCols(shift.get("src"), r["source_page"], shift, r["edited_at"] or r["submitted_at"],
                       r["seen_source_page"]),
            # Did the annotator find the commitment on the page the AI cited?
            # Empty = the item had no cited page, or was answered before the
            # check existed (2026-09-18).
            r["source_page_ok"] or "", r["source_page_fix"] or "", ai_final,
            r["seq"], r["letters"],
            round(r["priority"], 1), " | ".join(json.loads(r["flags"] or "[]")),
            r["stratum"], r["is_gold"],
            r["prolific_pid"], r["is_commitment"], r["not_reason"],
            r["commitment_fix"], r["target_year_fix"], human_final, agree,
            r["window_ok"], r["correct_target_year"], r["flips"], r["comment"],
            r["n_page_views"], r["task_seconds"], r["active_seconds"],
            r["submitted_at"], r["n_edits"], r["edited_at"],
            # The annotator's own star and note on the item (a working aid,
            # not an answer field; see DATASET_SCHEMA §3).
            r["starred"], r["note"],
        ])
    return buf.getvalue()


def YearCsv(conn):
    buf, w = _Writer([
        "item_id", "company", "industry", "commitment", "declared_year", "target_year",
        "year", "years_to_target", "ai_status", "human_status", "changed",
        "evidence_quality", "evidence_ok", "achieve_prob", "achieve_prob_num",
        "correct_page", "alt_clue", "alt_clue_text", "custom_basis",
        "evidence", "evidence_page", "evidence_page_orig", "evidence_page_seen", "page_offset",
        "evidence_page_kind", "is_gold", "prolific_pid", "assign_id",
    ])
    # ok = verdict confirmed as-is; current = wrong verdict but the quoted
    # evidence alone still holds as the basis. clue/mixed/other/none mean the
    # quoted evidence was not (only) it.
    ev_ok = {"ok", "current"}
    rows = conn.execute(
        """SELECT v.*, i.item_id, i.company, i.industry, i.commitment, i.declared_year,
                  i.target_year, i.payload, i.is_gold, wk.prolific_pid,
                  a.submitted_at, a.edited_at
             FROM verdict v
             JOIN assignment a ON a.assign_id = v.assign_id
             JOIN item i       ON i.item_id = a.item_id
             JOIN worker wk    ON wk.worker_id = a.worker_id
            WHERE a.stage = 3 AND wk.is_test = 0
            ORDER BY i.item_id, v.assign_id, v.year"""
    ).fetchall()
    cache = {}
    shifts = {}
    origs = {}
    for r in rows:
        if r["item_id"] not in cache:
            payload = json.loads(r["payload"])
            cache[r["item_id"]] = {y["year"]: y for y in common.YearRows(payload)}
            shifts[r["item_id"]] = _PageShift(r["payload"])
            # The clue texts before page_shift.py touched their page markers.
            origs[r["item_id"]] = {int(e["year"]): e["clues_orig"] for e in payload.get("status") or []
                                   if str(e.get("year")).isdigit() and e.get("clues_orig")}
        y = cache[r["item_id"]].get(r["year"], {})
        shift = shifts[r["item_id"]]
        yrec = (shift.get("years") or {}).get(str(r["year"])) or {}
        ty, yr = r["target_year"], r["year"]
        to_target = ty - yr if isinstance(ty, int) and isinstance(yr, int) else ""
        page_cols = _PageCols(yrec.get("evidence"), y.get("evidence_page", ""), shift,
                              r["edited_at"] or r["submitted_at"], r["seen_evidence_page"])
        # The clues as this annotator saw them: the original texts (old page
        # markers) when the answer was made against the original pages.
        clues = y.get("clues") or []
        if page_cols[0] != "" and str(page_cols[1]) == str(page_cols[0]) and origs[r["item_id"]].get(yr):
            clues = origs[r["item_id"]][yr]
        alt = r["alt_clue"] or ""
        # alt_clue lists every ticked basis ("current,1,3,custom"); the clue
        # indices among them are spelled out.
        picked = [b for b in alt.split(",") if b.isdigit() and 1 <= int(b) <= len(clues)]
        alt_text = " | ".join(clues[int(b) - 1] for b in picked)
        # Yearly labels in the current four names (rows written before the
        # rename hold 部分達成 on both sides).
        ai_status = common.DisplayStatus(r["ai_status"])
        human_status = common.DisplayStatus(r["human_status"])
        w.writerow([
            r["item_id"], r["company"], r["industry"], r["commitment"], r["declared_year"],
            ty, yr, to_target, ai_status, human_status,
            int(ai_status != human_status), r["evidence_quality"],
            int(r["evidence_quality"] in ev_ok), r["achieve_prob"],
            (int(r["achieve_prob"]) if (r["achieve_prob"] or "").isdigit()
             else PROB_RANK.get(r["achieve_prob"], "")),
            r["correct_page"], alt, alt_text, r["custom_basis"] or "",
            y.get("evidence", ""),
            y.get("evidence_page", ""),
            # As the pipeline wrote it / as this annotator saw it / how it was
            # moved onto the PDF page index (see page_shift.py, DATASET_SCHEMA §2b).
            *page_cols,
            r["is_gold"], r["prolific_pid"], r["assign_id"],
        ])
    return buf.getvalue()


def _Majority(values, rank=None):
    """Majority label; ties broken by the ordinal rank when one is supplied."""
    values = [v for v in values if v]
    if not values:
        return None, 0.0
    counts = Counter(values)
    top = max(counts.values())
    winners = sorted(k for k, c in counts.items() if c == top)
    if len(winners) > 1 and rank:
        winners.sort(key=lambda k: rank.get(k, 0))
        pick = winners[len(winners) // 2]
    else:
        pick = winners[0]
    return pick, top / len(values)


def RiskCsv(conn):
    """The step-4 dataset: features -> per-year probability trajectory -> outcome.

    prob_trajectory is the per-year median of the 0-100 estimates made after
    reading each year's clues -- forward-looking toward the target year,
    allowed to drift.
    human_outcome is the human-verified final record the trajectory should
    predict.
    """
    buf, w = _Writer([
        "item_id", "stratum", "is_gold", "company", "industry", "commitment", "declared_year",
        "target_year", "horizon_years", "resolved", "n_years_tracked", "has_number", "commitment_len",
        "n_annotators", "prob_trajectory", "prob_first", "prob_last", "prob_trend",
        "is_commitment_majority",
        "human_outcome", "outcome_agreement", "ai_final_status", "ai_letters",
        "ai_vs_human_final", "n_year_flips", "priority", "flags",
    ])
    # Submitted assignments by real annotators only (test accounts excluded).
    real = ("JOIN assignment a ON a.assign_id = {t}.assign_id "
            "JOIN worker w ON w.worker_id = a.worker_id AND w.is_test = 0")
    items = conn.execute(
        """SELECT i.* FROM item i
            WHERE EXISTS (SELECT 1 FROM assignment a JOIN worker w ON w.worker_id = a.worker_id
                           WHERE a.item_id = i.item_id AND a.stage = 3 AND w.is_test = 0)
            ORDER BY i.item_id"""
    ).fetchall()
    for it in items:
        antes = conn.execute(
            f"""SELECT an.* FROM ante an {real.format(t='an')}
                 WHERE a.item_id = ? AND a.stage = 3""", (it["item_id"],)).fetchall()
        finals = conn.execute(
            f"""SELECT f.* FROM final f {real.format(t='f')}
                 WHERE a.item_id = ? AND a.stage = 3""", (it["item_id"],)).fetchall()
        flips = conn.execute(
            f"""SELECT COUNT(*) AS n FROM verdict v {real.format(t='v')}
                 WHERE a.item_id = ? AND a.stage = 3 AND v.ai_status <> v.human_status""",
            (it["item_id"],)).fetchone()["n"]

        commit_majority, _ = _Majority([a["is_commitment"] for a in antes])

        # The per-year risk trajectory: median 0-100 rating per year (median is
        # robust with 3 annotators; legacy high/mid/low rows are skipped).
        traj = []
        for yr in conn.execute(
            f"""SELECT v.year, GROUP_CONCAT(v.achieve_prob) AS ps
                  FROM verdict v {real.format(t='v')}
                 WHERE a.item_id = ? AND a.stage = 3
              GROUP BY v.year ORDER BY v.year""", (it["item_id"],)):
            vals = sorted(int(p) for p in (yr["ps"] or "").split(",") if p.strip().isdigit())
            if vals:
                traj.append((yr["year"], vals[len(vals) // 2]))
        traj_text = ">".join(f"{y}:{p}" for y, p in traj)
        first_p = traj[0][1] if traj else ""
        last_p = traj[-1][1] if traj else ""
        trend = (last_p - first_p) if traj else ""
        # Every vote is mapped to the current three labels *before* counting,
        # so votes cast under the earlier names merge instead of splitting.
        outcome, outcome_agree = _Majority(
            [common.FinalStatus(f["human_final_status"]) for f in finals], STATUS_RANK)

        dy, ty = it["declared_year"], it["target_year"]
        horizon = ty - dy if isinstance(dy, int) and isinstance(ty, int) and ty >= dy else ""
        tracked = [y["year"] for y in common.YearRows(json.loads(it["payload"]))]
        # resolved=1: the reports already cover the target year, so the human
        # outcome label is final; 0 = still open (censored for the risk model).
        resolved = int(isinstance(ty, int) and bool(tracked) and ty <= max(tracked))
        w.writerow([
            it["item_id"], it["stratum"], it["is_gold"], it["company"], it["industry"], it["commitment"],
            dy, ty, horizon, resolved, it["n_years"], int(any(c.isdigit() for c in it["commitment"])),
            len(it["commitment"]), len(antes),
            traj_text, first_p, last_p, trend,
            commit_majority, outcome, round(outcome_agree, 2),
            common.FinalStatus(it["final_status"]), it["letters"],
            "" if outcome is None else int(common.FinalStatus(outcome) == common.FinalStatus(it["final_status"])),
            flips, round(it["priority"], 1),
            " | ".join(json.loads(it["flags"] or "[]")),
        ])
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--out_dir", default="exports")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    conn = common.Connect(args.db)
    common.Migrate(conn)  # tables added later (mark) exist even on an older DB
    for name, fn in (("item", ItemCsv), ("year", YearCsv), ("risk", RiskCsv)):
        path = os.path.join(args.out_dir, f"esg_{name}.csv")
        text = fn(conn)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        print(f"{path}: {text.count(chr(10)) - 1} rows")


if __name__ == "__main__":
    main()
