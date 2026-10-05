#!/usr/bin/env bash
# Code/template change: back up the DB -> add any new columns -> restart
# gunicorn -> verify -> report.
#
# How this differs from restart_web.sh: that one also runs MarkCalibration /
# LoadGold and Allocate, which is what you want after a *config* change. This
# script runs neither, so the calibration set and the allocation table are never
# recomputed. The only thing here that touches the database is common.Migrate,
# and it only ADDS columns -- no existing row is modified, old rows simply hold
# NULL in the new column, and running it again is a no-op.
#
#   bash reload_web.sh
#
# The address does not change and the tunnel is left alone. There is a 1-2 second
# gap while the workers swap; the task page's autosave retries by itself, so
# nothing typed is lost.
set -euo pipefail
cd "$(dirname "$0")"
PORT=${ESG_PORT:-8081}

# shellcheck disable=SC1091
source env.prod.sh
mkdir -p logs

echo "--- 1/4 database backup (online snapshot, annotators unaffected) ---"
.venv/bin/python backup_db.py

echo
echo "--- 2/4 can the new code be imported? (if not, nothing is restarted) ---"
# Import it in a separate process first, so a syntax or import error stops here
# instead of taking the service down.
# Also cross-check every endpoint the templates actually url_for() against the
# route table: the MyStats 500s were a template naming an endpoint the running
# code did not have, and that kind of error only surfaces when someone opens
# the page.
.venv/bin/python -c "
import pathlib, re, sys
import app
names = {r.endpoint for r in app.app.url_map.iter_rules()}
used = {}
for p in sorted(pathlib.Path('templates').rglob('*.html')):
    for m in re.findall(r\"url_for\(\s*'([A-Za-z_][A-Za-z0-9_]*)'\", p.read_text(encoding='utf-8')):
        used.setdefault(m, set()).add(p.name)
bad = {e: sorted(f) for e, f in used.items() if e not in names}
floor = {'Index', 'Task', 'Post', 'Draft', 'MyStats', 'PdfExcerpt', 'ReportPdf',
         'PageImage', 'Locate', 'History', 'Admin', 'AdminAnnotators'} - names
for e, files in sorted(bad.items()):
    print(f'  template {\",\".join(files)} uses an endpoint that does not exist: {e}', file=sys.stderr)
if floor:
    print('  missing routes: ' + ', '.join(sorted(floor)), file=sys.stderr)
print(f'  all {len(used)} endpoints referenced by templates exist')
raise SystemExit(1 if (bad or floor) else 0)
"
echo "code and routes OK"

echo
echo "--- 2.5/4 add any new database columns (adds only; no row is changed) ---"
# ALTER TABLE ... ADD COLUMN and nothing else: no re-pick of the calibration
# set, no re-allocation, no answer touched.
.venv/bin/python -c "
import common
conn = common.Connect()
added = common.Migrate(conn)
print('  added:', ', '.join(added) if added else '(already up to date)')
conn.close()
"

echo
echo "--- 3/4 restart gunicorn (this instance only) ---"
bash run.sh stop-web || true
sleep 1
bash run.sh start-web

for _ in $(seq 1 20); do
  sleep 1
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
done

echo
echo "--- 4/4 verify every worker is running the new version ---"
gpid=$(bash run.sh pid) || { echo "gunicorn did not start (tail logs/gunicorn.log)"; exit 1; }
tr '\0' '\n' < "/proc/${gpid}/environ" | grep '^ESG_PID_ALLOWLIST=.' >/dev/null \
  && echo "allowlist loaded OK" || { echo "allowlist NOT loaded (tail logs/gunicorn.log)"; exit 1; }

# healthz only proves something is listening, and an unauthenticated /mystats
# returns its 302 before any template runs, so it proves nothing at all. Log in
# with the test account's cookie instead and fetch a page only a logged-in
# annotator sees, checking that base.html rendered all the way to its footer --
# a BuildError halfway through comes back as a 500. With -w 4, 12 requests give
# each worker a turn.
jar=$(mktemp); trap 'rm -f "$jar" logs/check.html' EXIT
test_pid=${ESG_TEST_PIDS%%,*}
curl -s -c "$jar" -o /dev/null "http://127.0.0.1:${PORT}/?pid=${test_pid:-self-check}"
fail=0 consented=0
for _ in $(seq 1 12); do
  code=$(curl -s -b "$jar" -c "$jar" -L -o logs/check.html \
              -w '%{http_code}' "http://127.0.0.1:${PORT}/instructions")
  [ "$code" = "200" ] || { echo "  /instructions -> HTTP ${code}"; fail=1; continue; }
  grep 'ESG commitment verification study' logs/check.html >/dev/null \
    || { echo "  /instructions returned 200 but base.html did not finish rendering"; fail=1; }
  grep 'href="/mystats"' logs/check.html >/dev/null && consented=1
done
[ "$fail" = 0 ] && echo "every worker renders a logged-in page OK" \
                || { echo "a worker failed to render (tail logs/gunicorn.log)"; exit 1; }
[ "$consented" = 1 ] \
  && echo "url_for('MyStats') in the header resolves OK" \
  || echo "note: the test account has not consented, so the header's stats link was not exercised"

echo
echo "Done. Same address, tunnel untouched."
echo "Now open one item by hand and check its excerpt and full-report links."
