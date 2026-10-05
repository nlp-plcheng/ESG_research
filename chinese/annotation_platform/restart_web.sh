#!/usr/bin/env bash
# 套用程式／設定修改：資料庫遷移 -> 只重啟「本實例」的 gunicorn（tmux web 視窗）-> 驗證白名單 -> 重印連結
# tunnel 不動、網址不變。
set -euo pipefail
cd "$(dirname "$0")"
PORT=8080
SESSION=${ESG_SESSION:-esg}

# 先套用資料庫欄位遷移（只會「新增」欄位，既有資料不動，可重複執行）
# shellcheck disable=SC1091
source env.prod.sh
python3 - <<'EOF'
import allocate, common, config, ingest
conn = common.Connect()
added = common.Migrate(conn)
print("DB migrate:", ",".join(added) if added else "已是最新")
conn.execute("BEGIN")
if config.GOLD_FILE:
    # A curated set (ESG_GOLD_FILE) is re-applied as-is; the automatic pick
    # must never overwrite it on a restart.
    import json
    want = len(json.load(open(config.GOLD_FILE, encoding="utf-8")))
    hit = ingest.LoadGold(conn, config.GOLD_FILE)
    print(f"校準題（ESG_GOLD_FILE 自訂清單）: {hit}/{want}")
    if hit < want:
        conn.execute("ROLLBACK")
        raise SystemExit("✗ 自訂校準題有對不上的項目（見上方 gold not matched）；沒有改動任何東西，服務也沒重啟")
else:
    print(f"校準題（異常分數最高的前 {config.CALIBRATION_N} 題）:",
          ingest.MarkCalibration(conn, config.CALIBRATION_N))
allocate.Allocate(conn, config.N_ANNOTATORS, config.REDUNDANCY, config.REDUNDANCY_CONTROL)
conn.execute("COMMIT")
allocate.PrintSummary(conn)
EOF

# 只認完全相同的名稱（tmux 自己的 -t 比對會接受前綴，例如 esg 會對到 esg-en）
tmux list-sessions -F '#S' 2>/dev/null | grep -x "$SESSION" >/dev/null || { echo "tmux session「${SESSION}」不存在，請改跑 bash launch.sh"; exit 1; }
# 只停本實例的 gunicorn、只用本實例的 tmux session：run.sh 以「工作目錄＋port」核對，
# 同機的其他平台不會被誤殺
bash run.sh stop-web || true
sleep 1
bash run.sh start-web

for _ in $(seq 1 15); do
  sleep 1
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
done

echo "--- 驗證 ---"
gpid=$(bash run.sh pid) || { echo "✗ gunicorn 沒起來（tail logs/gunicorn.log）"; exit 1; }
# （這幾條 pipe 的 grep 不用 -q：-q 提早結束會讓上游收到 SIGPIPE，配合 pipefail 就變成假的失敗）
tr '\0' '\n' < "/proc/${gpid}/environ" | grep '^ESG_PID_ALLOWLIST=.' >/dev/null \
  && echo "白名單載入 ✓" || { echo "白名單沒載入 ✗（tail logs/gunicorn.log 貼給 Claude）"; exit 1; }
curl -s "http://127.0.0.1:${PORT}/?pid=__nobody__" | grep "沒有權限" >/dev/null \
  && echo "名單外被擋 ✓" || { echo "名單外沒被擋 ✗"; exit 1; }
# An admitted pid either gets the consent page (200, "同意") or, once it has
# consented, a 302 straight to the instructions -- both mean "recognised".
test_pid=${ESG_TEST_PIDS%%,*}
code=$(curl -s -o logs/check.html -w '%{http_code}' "http://127.0.0.1:${PORT}/?pid=${test_pid:-self-check}")
if [ "$code" = "302" ] || grep -q "同意" logs/check.html; then
  echo "名單內可進入 ✓"
else
  echo "名單內進不去 ✗（HTTP ${code}；ESG_ALLOW_LOCAL_PID 是 1 嗎？）"; exit 1
fi

echo
echo "網址不變。"
bash run.sh links
