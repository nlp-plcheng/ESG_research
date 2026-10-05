"""Wipe annotation data at three different granularities.

    python reset.py --answers            # clear all answers, keep the ingested items
    python reset.py --worker test01      # clear one annotator so they can redo the items
    python reset.py --all                # delete the whole DB file (items included)

Always prints what it is about to destroy and asks for confirmation, unless
--yes is given. Page images under static/pages/ are a pure cache and are left
alone; delete that directory by hand if you want to force a re-render.
"""

import argparse
import os

import common
import config

# Child tables first: assignment is referenced by all of them.
ANSWER_TABLES = ("verdict", "final", "ante", "draft", "mark", "assignment")


def Counts(conn):
    out = {}
    for t in ("item", "worker", "assignment", "ante", "verdict", "final", "draft", "mark"):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        except Exception:
            out[t] = 0
    return out


def Confirm(prompt, auto_yes):
    if auto_yes:
        return True
    return input(f"{prompt} [yes/NO] ").strip() == "yes"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=config.DB_PATH)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--answers", action="store_true",
                       help="delete every answer and worker, keep items")
    group.add_argument("--worker", help="delete one annotator's answers by participant id")
    group.add_argument("--all", action="store_true",
                       help="delete the DB file entirely (re-run ingest.py afterwards)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if args.all:
        removed = []
        for suffix in ("", "-wal", "-shm"):
            path = args.db + suffix
            if os.path.exists(path):
                removed.append(path)
        if not removed:
            print(f"nothing to delete at {args.db}")
            return
        print("about to delete:")
        for path in removed:
            print(f"  {path}  ({os.path.getsize(path) / 1e6:.1f} MB)")
        if not Confirm("This destroys items AND every annotation. Proceed?", args.yes):
            raise SystemExit("aborted")
        for path in removed:
            os.remove(path)
        print("deleted. run ingest.py again before starting the app.")
        return

    if not os.path.isfile(args.db):
        raise SystemExit(f"no database at {args.db}")
    conn = common.Connect(args.db)
    common.Migrate(conn)  # every child table exists, so each DELETE below is valid
    before = Counts(conn)

    if args.worker:
        row = conn.execute("SELECT worker_id FROM worker WHERE prolific_pid = ?",
                           (args.worker,)).fetchone()
        if row is None:
            raise SystemExit(f"no such annotator: {args.worker}")
        wid = row["worker_id"]
        n = conn.execute("SELECT COUNT(*) AS n FROM assignment WHERE worker_id = ?",
                         (wid,)).fetchone()["n"]
        print(f"annotator {args.worker}: {n} assignments will be released back to the pool")
        if not Confirm("Proceed?", args.yes):
            raise SystemExit("aborted")
        conn.execute("BEGIN")
        for t in ("verdict", "final", "ante", "draft", "mark"):
            conn.execute(f"DELETE FROM {t} WHERE assign_id IN "
                         "(SELECT assign_id FROM assignment WHERE worker_id = ?)", (wid,))
        conn.execute("DELETE FROM assignment WHERE worker_id = ?", (wid,))
        conn.execute("DELETE FROM worker WHERE worker_id = ?", (wid,))
        conn.execute("COMMIT")
    else:
        print(f"about to delete every answer in {args.db}:")
        for t in ("worker", "assignment", "ante", "verdict", "final", "draft", "mark"):
            print(f"  {t:<12} {before[t]}")
        print(f"  {'item':<12} {before['item']}  (kept)")
        if not Confirm("Proceed?", args.yes):
            raise SystemExit("aborted")
        conn.execute("BEGIN")
        for t in ANSWER_TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM worker")
        conn.execute("COMMIT")

    conn.execute("VACUUM")
    after = Counts(conn)
    print("\ntable         before   after")
    for t in sorted(before):
        print(f"{t:<12} {before[t]:>7} {after[t]:>7}")


if __name__ == "__main__":
    main()
