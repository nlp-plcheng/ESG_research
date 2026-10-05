#!/usr/bin/env bash
# Apply code/config changes: migrate the DB -> restart THIS instance's gunicorn
# (tmux web window) -> verify the allowlist -> print the links again. The
# tunnel is left alone, so the public address does not change.
set -euo pipefail
cd "$(dirname "$0")"
PORT=${ESG_PORT:-8081}          # same rule as run.sh: shell variables, not env.prod.sh settings
SESSION=${ESG_SESSION:-esg-en}

# Database migration first (only ever ADDS columns; existing data untouched;
# safe to run repeatedly).
# shellcheck disable=SC1091
source env.prod.sh
python3 - <<'EOF'
import allocate, common, config, ingest
conn = common.Connect()
added = common.Migrate(conn)
print("DB migrate:", ",".join(added) if added else "up to date")
conn.execute("BEGIN")
if config.GOLD_FILE:
    # A curated set (ESG_GOLD_FILE) is re-applied as-is; the automatic pick
    # must never overwrite it on a restart.
    import json
    want = len(json.load(open(config.GOLD_FILE, encoding="utf-8")))
    hit = ingest.LoadGold(conn, config.GOLD_FILE)
    print(f"calibration items (curated list, ESG_GOLD_FILE): {hit}/{want}")
    if hit < want:
        conn.execute("ROLLBACK")
        raise SystemExit("x some curated calibration items did not match (see 'gold not matched' above); nothing changed, service not restarted")
else:
    print(f"calibration items (top {config.CALIBRATION_N} by anomaly score):",
          ingest.MarkCalibration(conn, config.CALIBRATION_N))
allocate.Allocate(conn, config.N_ANNOTATORS, config.REDUNDANCY, config.REDUNDANCY_CONTROL)
conn.execute("COMMIT")
allocate.PrintSummary(conn)
EOF

# Exact name (tmux's own -t matching also accepts prefixes, e.g. esg -> esg-en).
tmux list-sessions -F '#S' 2>/dev/null | grep -x "$SESSION" >/dev/null || { echo "tmux session '${SESSION}' does not exist -- run bash launch.sh instead"; exit 1; }
# Only this instance's gunicorn is stopped and only its own tmux session is
# used: run.sh verifies both by working directory (and the port), so another
# platform on the machine is never hit.
bash run.sh stop-web || true
sleep 1
bash run.sh start-web

for _ in $(seq 1 15); do
  sleep 1
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
done

echo "--- checks ---"
gpid=$(bash run.sh pid) || { echo "x gunicorn did not start (tail logs/gunicorn.log)"; exit 1; }
# (grep without -q on these pipes: -q exits early, the writer gets SIGPIPE and
# pipefail would turn that into a false failure)
tr '\0' '\n' < "/proc/${gpid}/environ" | grep '^ESG_PID_ALLOWLIST=.' >/dev/null \
  && echo "allowlist loaded  OK" || { echo "x allowlist not loaded (tail logs/gunicorn.log)"; exit 1; }
curl -s "http://127.0.0.1:${PORT}/?pid=__nobody__" | grep "have access" >/dev/null \
  && echo "unknown id blocked  OK" || { echo "x unknown id NOT blocked"; exit 1; }
# An admitted id either gets the consent page (200, "agree") or, once it has
# consented, a 302 straight to the instructions -- both mean "recognised".
test_pid=${ESG_TEST_PIDS%%,*}
code=$(curl -s -o logs/check.html -w '%{http_code}' "http://127.0.0.1:${PORT}/?pid=${test_pid:-self-check}")
if [ "$code" = "302" ] || grep -q "agree" logs/check.html; then
  echo "test account admitted  OK"
else
  echo "x test account NOT admitted (HTTP ${code}; is ESG_ALLOW_LOCAL_PID=1?)"; exit 1
fi

echo
echo "public address unchanged."
bash run.sh links
