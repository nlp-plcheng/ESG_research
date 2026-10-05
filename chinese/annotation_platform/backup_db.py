"""A consistent snapshot of the live database, taken while the service runs.

    python backup_db.py                    # -> <db folder>/backups/annotation-<UTC>.db
    python backup_db.py --out /path/to.db

Uses SQLite's online backup API, so it is safe with annotators mid-answer: the
copy is a single consistent point in time. Copying the .db file with `cp` is
NOT safe here -- the database runs in WAL mode, so the newest committed answers
live in the -wal file and a plain copy can miss them or capture a torn page.

Take one before every deployment. It only reads, so it can be run any time.
"""

import argparse
import os
import sqlite3
import time

import config

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--db", default=config.DB_PATH)
ap.add_argument("--out", default=None, help="target file (default: <db folder>/backups/...)")
args = ap.parse_args()

out = args.out or os.path.join(
    os.path.dirname(os.path.abspath(args.db)), "backups",
    f"annotation-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db")

# The copy holds every answer and every participant id, so it must never exist
# -- not even for the seconds the copy takes, and not if the copy fails
# halfway -- with group/other read. The umask covers the journal and WAL files
# SQLite creates alongside it, which chmod afterwards would miss.
os.umask(0o077)
os.makedirs(os.path.dirname(os.path.abspath(out)), mode=0o700, exist_ok=True)
os.chmod(os.path.dirname(os.path.abspath(out)), 0o700)

src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
dst = sqlite3.connect(out)
try:
    with dst:
        src.backup(dst)
finally:
    dst.close()

# Row counts of the answer tables, as the receipt that the copy is not empty.
check = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
counts = {t: check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("worker", "item", "assignment", "ante", "verdict", "final", "draft")}
submitted = check.execute("SELECT COUNT(*) FROM assignment WHERE stage = 3").fetchone()[0]
check.close()
src.close()

mode = os.stat(out).st_mode & 0o777
if mode & 0o077:                       # belt and braces: the umask should have done it
    os.chmod(out, 0o600)
    mode = 0o600
print(f"{out}  ({os.path.getsize(out) / 1e6:.1f} MB, mode {mode:04o})")
print("  " + ", ".join(f"{t} {n}" for t, n in counts.items()))
print(f"  submitted answers (stage 3): {submitted}")
