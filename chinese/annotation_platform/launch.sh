#!/usr/bin/env bash
# 一鍵開站：補金鑰 -> 重建題庫(僅在安全時) -> 起服務 -> 驗證白名單 -> 印出每人專屬連結
set -euo pipefail
cd "$(dirname "$0")"
PORT=8080

[ -f env.prod.sh ] || { echo "env.prod.sh 不存在：先 bash run.sh prod-init（或 cp env.prod.sh.example env.prod.sh）並填 ESG_PID_ALLOWLIST"; exit 1; }

# 金鑰空值自動補齊（不會動到已填好的值，可重複執行）。
# 判斷依據是 shell 實際載入的值，不是那一行的文字——範本裡「=」後面只有空白和註解，
# 載入後仍是空字串；補完後重新載入再確認一次。
# shellcheck disable=SC1091
source env.prod.sh 2>/dev/null
Fill() {  # Fill 變數 值：整行「export 變數=...」換掉
  grep -q "^export $1=" env.prod.sh || { echo "✗ env.prod.sh 沒有「export $1=」這一行（對照 env.prod.sh.example）"; exit 1; }
  sed -i "s|^export $1=.*|export $1=$2|" env.prod.sh
  echo "・已自動填入 $1"
}
[ -n "${ESG_SECRET_KEY:-}" ]    || Fill ESG_SECRET_KEY "$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
[ -n "${ESG_ADMIN_KEY:-}" ]     || Fill ESG_ADMIN_KEY "$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
# 完成代碼只補一個隨機暫代值，絕不寫死任何固定字串：真正的 completion code 是 Prolific
# 後台那一個，只該出現在 env.prod.sh 裡。
[ -n "${ESG_PROLIFIC_CODE:-}" ] || { Fill ESG_PROLIFIC_CODE "$(python3 -c 'import secrets;print(secrets.token_hex(4).upper())')"; echo "  （隨機暫代值；Prolific 收案前請改成 Prolific 後台的 completion code）"; }

# shellcheck disable=SC1091
source env.prod.sh
[ -n "${ESG_SECRET_KEY:-}" ] && [ -n "${ESG_ADMIN_KEY:-}" ] \
  || { echo "✗ 補完之後 ESG_SECRET_KEY / ESG_ADMIN_KEY 還是空的，檢查 env.prod.sh"; exit 1; }
[ -n "${ESG_PID_ALLOWLIST:-}" ] || { echo "ESG_PID_ALLOWLIST 是空的：在 env.prod.sh 填入代號後重跑"; exit 1; }
set +u
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || { echo "還沒 setup：先跑 bash run.sh setup"; exit 1; }
set -u

# 題庫：只有在「還沒有任何已送出作答」時才重建，避免誤刪已收資料
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
  echo "・重建題庫（目前沒有任何已送出作答，安全）"
  python reset.py --all --yes >/dev/null 2>&1 || true
  python ingest.py   # 含：標記校準題（異常分數最高的前 ESG_CALIBRATION_N 題）＋ 等量分配（若已設 ESG_N_ANNOTATORS）
else
  echo "・偵測到已有 ${submitted} 份作答：保留資料、不重建題庫"
fi

bash run.sh serve

base=$(grep -oE 'https://[a-z0-9.-]+\.trycloudflare\.com' logs/tunnel.log | tail -1) || true
[ -n "${base:-}" ] || { echo "拿不到 tunnel 網址：稍等幾秒跑 bash run.sh url"; exit 1; }

echo "--- 驗證 ---"
# 只認本實例的 gunicorn（run.sh 以工作目錄＋port 核對 pid），同機的其他平台不會被誤認
gpid=$(bash run.sh pid) || true
[ -n "${gpid:-}" ] || { echo "✗ gunicorn 沒起來（看 logs/gunicorn.log）"; exit 1; }
# （這幾條 pipe 的 grep 不用 -q：-q 提早結束會讓上游收到 SIGPIPE，配合 pipefail 就變成假的失敗）
tr '\0' '\n' < "/proc/${gpid}/environ" | grep '^ESG_PID_ALLOWLIST=.' >/dev/null \
  && echo "白名單載入 ✓" || { echo "白名單沒載入 ✗"; exit 1; }
curl -s "http://127.0.0.1:${PORT}/?pid=__nobody__" | grep "沒有權限" >/dev/null \
  && echo "名單外被擋 ✓" || { echo "名單外沒被擋 ✗"; exit 1; }
first=${ESG_PID_ALLOWLIST%%,*}
# An admitted pid either gets the consent page (200, "同意") or, once it has
# consented, a 302 straight to the instructions -- both mean "recognised".
code=$(curl -s -o logs/check.html -w '%{http_code}' "http://127.0.0.1:${PORT}/?pid=${first}")
if [ "$code" = "302" ] || grep -q "同意" logs/check.html; then
  echo "名單內可進入 ✓"
else
  echo "名單內進不去 ✗（HTTP ${code}；ESG_ALLOW_LOCAL_PID 是 1 嗎？）"; exit 1
fi

echo
bash run.sh links
echo "收案期間別重跑 serve、別動 tmux、機器別重開（沒設固定連結頁的話網址會變）。"
# 不要用 cp 備份：DB 跑在 WAL 模式，最新的作答在 -wal 檔裡，直接複製 .db 可能少資料
# 或抓到寫到一半的頁面。backup_db.py 用 SQLite 的線上備份 API，作答中也能跑。
echo "隨時匯出：bash run.sh export ／ 備份：.venv/bin/python backup_db.py"
