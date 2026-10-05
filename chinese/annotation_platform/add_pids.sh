#!/usr/bin/env bash
# 用法：bash add_pids.sh [數量]   （預設 5）
# 產生 N 組亂碼 pid 加進白名單；服務在跑就用 restart_web.sh 套用、驗證並重印連結。
# 注意：ESG_N_ANNOTATORS > 0 時只有白名單前 N 個（非測試）pid 能進來，單純加 pid
# 不會多出名額——要加人請一併調高 env.prod.sh 的 ESG_N_ANNOTATORS。
set -euo pipefail
cd "$(dirname "$0")"
n=${1:-5}

[ -f env.prod.sh ] || { echo "env.prod.sh 不存在"; exit 1; }
grep -q '^export ESG_PID_ALLOWLIST=' env.prod.sh || { echo "env.prod.sh 裡沒有 ESG_PID_ALLOWLIST 那行"; exit 1; }

new=$(python3 - "$n" <<'EOF'
import secrets, sys
print(",".join("w" + secrets.token_hex(5) for _ in range(int(sys.argv[1]))))
EOF
)
sed -i "s|^export ESG_PID_ALLOWLIST=.*|&,${new}|" env.prod.sh
echo "・已把 ${n} 組新 pid 寫進 env.prod.sh 白名單"

if tmux list-sessions -F '#S' 2>/dev/null | grep -x "${ESG_SESSION:-esg}" >/dev/null; then
  bash restart_web.sh
else
  echo "・服務還沒在跑：之後 launch.sh 啟動時生效（連結用 bash run.sh links 印）"
fi
