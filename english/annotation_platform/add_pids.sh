#!/usr/bin/env bash
# Usage: bash add_pids.sh [count]   (default 5)
# Generates N random participant ids and appends them to the allowlist in
# env.prod.sh; if the service is running, restart_web.sh applies them and
# prints the links. Note: with ESG_N_ANNOTATORS > 0 only the first N non-test
# ids in the allowlist can enter -- adding ids does not add seats; raise
# ESG_N_ANNOTATORS as well when you add people.
set -euo pipefail
cd "$(dirname "$0")"
n=${1:-5}

[ -f env.prod.sh ] || { echo "env.prod.sh missing"; exit 1; }
grep -q '^export ESG_PID_ALLOWLIST=' env.prod.sh || { echo "no ESG_PID_ALLOWLIST line in env.prod.sh"; exit 1; }

new=$(python3 - "$n" <<'EOF'
import secrets, sys
print(",".join("w" + secrets.token_hex(5) for _ in range(int(sys.argv[1]))))
EOF
)
sed -i "s|^export ESG_PID_ALLOWLIST=.*|&,${new}|" env.prod.sh
echo "- added ${n} new ids to the allowlist in env.prod.sh"

if tmux list-sessions -F '#S' 2>/dev/null | grep -x "${ESG_SESSION:-esg-en}" >/dev/null; then
  bash restart_web.sh
else
  echo "- service not running: the ids take effect at the next launch (bash run.sh links prints them)"
fi
