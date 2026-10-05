"""Balanced pre-allocation of items to annotator slots.

With ESG_N_ANNOTATORS = N, every active non-calibration item is assigned to
REDUNDANCY distinct slots 0..N-1 so that (a) per-slot item counts differ by at
most one and (b) per-slot priority-score totals stay close: items are handed
out in descending priority, each to the emptiest slots (fewest items first,
then lowest score). A slot that already holds an assignment for an item keeps
it, so re-running after N changes never moves work someone has started.

    python allocate.py          # (re)allocate with the current config
    python allocate.py --show   # print per-slot counts / score sums only
"""

import argparse
from collections import defaultdict

import common
import config


def Allocate(conn, n_slots, redundancy, redundancy_control=None):
    """redundancy = people per anomaly item; redundancy_control = people per
    control item (defaults to the same)."""
    if redundancy_control is None:
        redundancy_control = redundancy
    if n_slots <= 0:
        conn.execute("DELETE FROM alloc")
        return
    if max(redundancy, redundancy_control) > n_slots:
        raise SystemExit(f"每題人數 ({redundancy}/{redundancy_control}) 不能大於 "
                         f"ESG_N_ANNOTATORS ({n_slots})")

    items = conn.execute(
        "SELECT item_id, priority, stratum FROM item WHERE active = 1 AND is_gold = 0 "
        "ORDER BY priority DESC, item_id").fetchall()
    prio = {r["item_id"]: r["priority"] for r in items}

    # (item, slot) pairs someone in that slot has already opened or submitted.
    locked = defaultdict(set)
    for r in conn.execute(
            """SELECT DISTINCT a.item_id, w.slot FROM assignment a
                 JOIN worker w ON w.worker_id = a.worker_id
                WHERE w.is_test = 0 AND w.slot IS NOT NULL"""):
        if 0 <= r["slot"] < n_slots and r["item_id"] in prio:
            locked[r["item_id"]].add(r["slot"])

    count = [0] * n_slots
    score = [0.0] * n_slots
    for item_id, slots in locked.items():
        for s in slots:
            count[s] += 1
            score[s] += prio[item_id]

    rows = []
    for r in items:
        have = locked.get(r["item_id"], set())
        want = redundancy if r["stratum"] == "anomaly" else redundancy_control
        need = want - len(have)
        free = sorted((s for s in range(n_slots) if s not in have),
                      key=lambda s: (count[s], score[s], s))
        chosen = free[:need] if need > 0 else []
        for s in chosen:
            count[s] += 1
            score[s] += r["priority"]
        rows.extend((r["item_id"], s) for s in have | set(chosen))

    conn.execute("DELETE FROM alloc")
    conn.executemany("INSERT INTO alloc (item_id, slot) VALUES (?, ?)", rows)


def Summary(conn):
    return conn.execute(
        """SELECT al.slot, COUNT(*) AS n, ROUND(SUM(i.priority), 1) AS score
             FROM alloc al JOIN item i ON i.item_id = al.item_id
            WHERE i.active = 1
         GROUP BY al.slot ORDER BY al.slot""").fetchall()


def PrintSummary(conn):
    rows = Summary(conn)
    if not rows:
        print("未分配（ESG_N_ANNOTATORS = 0，先到先做）")
        return
    for r in rows:
        print(f"  槽位 {r['slot']:>2}: {r['n']:>5} 題   分數總和 {r['score']:>9}")
    ns = [r["n"] for r in rows]
    print(f"  題數 {min(ns)}–{max(ns)}（差 {max(ns) - min(ns)}），共 {len(rows)} 個槽位")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--n", type=int, default=config.N_ANNOTATORS, help="總作答人數")
    ap.add_argument("--show", action="store_true", help="只顯示目前的分配")
    args = ap.parse_args()

    conn = common.Connect(args.db)
    if not args.show:
        conn.execute("BEGIN")
        Allocate(conn, args.n, config.REDUNDANCY, config.REDUNDANCY_CONTROL)
        conn.execute("COMMIT")
    PrintSummary(conn)


if __name__ == "__main__":
    main()
