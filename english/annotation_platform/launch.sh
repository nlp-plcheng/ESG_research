#!/usr/bin/env bash
# One-shot launch: fill in missing secrets -> (re)build the item bank when safe ->
# start the service -> verify the allowlist -> print every personal link.
set -euo pipefail
cd "$(dirname "$0")"
PORT=${ESG_PORT:-8081}   # same rule as run.sh: a shell variable, not an env.prod.sh setting

[ -f env.prod.sh ] || { echo "env.prod.sh missing: run bash run.sh prod-init (or cp env.prod.sh.example env.prod.sh) and fill in ESG_PID_ALLOWLIST"; exit 1; }

# Empty secrets are generated automatically; filled-in values are never touched.
# Decided on the values the shell actually loads -- in the example the line
# holds only spaces and a comment after the "=", which still loads as an empty
# value -- and verified again after a reload.
# shellcheck disable=SC1091
source env.prod.sh 2>/dev/null
Fill() {  # Fill VAR VALUE: replace the whole "export VAR=..." line
  grep -q "^export $1=" env.prod.sh || { echo "x env.prod.sh has no 'export $1=' line (see env.prod.sh.example)"; exit 1; }
  sed -i "s|^export $1=.*|export $1=$2|" env.prod.sh
  echo "- filled in $1"
}
[ -n "${ESG_SECRET_KEY:-}" ]    || Fill ESG_SECRET_KEY "$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
[ -n "${ESG_ADMIN_KEY:-}" ]     || Fill ESG_ADMIN_KEY "$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
# The completion code gets a random stand-in, never a fixed word: the real
# one is whatever the Prolific study says, and it belongs in env.prod.sh only.
[ -n "${ESG_PROLIFIC_CODE:-}" ] || { Fill ESG_PROLIFIC_CODE "$(python3 -c 'import secrets;print(secrets.token_hex(4).upper())')"; echo "  (random stand-in; set the study's real completion code before a Prolific run)"; }
# shellcheck disable=SC1091
source env.prod.sh
[ -n "${ESG_SECRET_KEY:-}" ] && [ -n "${ESG_ADMIN_KEY:-}" ] \
  || { echo "x ESG_SECRET_KEY / ESG_ADMIN_KEY are still empty after filling them in -- check env.prod.sh"; exit 1; }
[ -n "${ESG_PID_ALLOWLIST:-}" ] || { echo "ESG_PID_ALLOWLIST is empty: add participant ids to env.prod.sh and run again"; exit 1; }
set +u
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || { echo "not set up yet: run bash run.sh setup first"; exit 1; }
set -u

# The item bank is rebuilt only while no answer has been submitted yet, so
# collected data can never be wiped by accident.
submitted=$(python3 - <<'EOF'
import common
try:
    conn = common.Connect()
    print(conn.execute("SELECT COUNT(*) FROM assignment WHERE stage=3").fetchone()[0])
except Exception:
    print(0)
EOF
)
if [ "$submitted" = "0" ]; then
  echo "- rebuilding the item bank (no submitted answers yet, safe)"
  python reset.py --all --yes >/dev/null 2>&1 || true
  python ingest.py   # also marks the calibration set (top ESG_CALIBRATION_N by anomaly score)
                     # and, with ESG_N_ANNOTATORS, allocates items evenly
else
  echo "- ${submitted} answers already submitted: keeping the data, not rebuilding"
fi

bash run.sh serve

base=$(grep -oE 'https://[a-z0-9.-]+\.trycloudflare\.com' logs/tunnel.log | tail -1) || true
[ -n "${base:-}" ] || { echo "no tunnel address yet: run bash run.sh url in a few seconds"; exit 1; }

echo "--- checks ---"
# This instance's gunicorn only (run.sh verifies the pid by working directory
# and port): another platform on the same machine must not be mistaken for it.
gpid=$(bash run.sh pid) || true
[ -n "${gpid:-}" ] || { echo "x gunicorn did not start (see logs/gunicorn.log)"; exit 1; }
# (grep without -q on these pipes: -q exits early, the writer gets SIGPIPE and
# pipefail would turn that into a false failure)
tr '\0' '\n' < "/proc/${gpid}/environ" | grep '^ESG_PID_ALLOWLIST=.' >/dev/null \
  && echo "allowlist loaded  OK" || { echo "x allowlist not loaded"; exit 1; }
curl -s "http://127.0.0.1:${PORT}/?pid=__nobody__" | grep "have access" >/dev/null \
  && echo "unknown id blocked  OK" || { echo "x unknown id NOT blocked"; exit 1; }
first=${ESG_PID_ALLOWLIST%%,*}
# An admitted id either gets the consent page (200, "agree") or, once it has
# consented, a 302 straight to the instructions -- both mean "recognised".
code=$(curl -s -o logs/check.html -w '%{http_code}' "http://127.0.0.1:${PORT}/?pid=${first}")
if [ "$code" = "302" ] || grep -q "agree" logs/check.html; then
  echo "listed id admitted  OK"
else
  echo "x listed id NOT admitted (HTTP ${code}; is ESG_ALLOW_LOCAL_PID=1?)"; exit 1
fi

echo
bash run.sh links
echo "While collecting: do not re-run serve, do not touch the tmux session, do not reboot (the tunnel address would change unless a fixed link page is set up)."
# Never `cp` the database: it runs in WAL mode, so the newest answers live in
# the -wal file and a plain copy can miss them or catch a half-written page.
# backup_db.py uses SQLite's online backup API and is safe mid-answer.
echo "Export any time: bash run.sh export   Backup: .venv/bin/python backup_db.py"
