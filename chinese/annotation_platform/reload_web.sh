#!/usr/bin/env bash
# 換程式／模板：備份 DB -> 補欄位 -> 重啟 gunicorn -> 驗證 -> 印出結果。
#
# 和 restart_web.sh 的差別：restart_web.sh 還會跑 MarkCalibration／LoadGold 和
# Allocate（改設定時才需要）。這支不碰那兩個，所以校準題和分配表不會被重算。
# 這支唯一會動到 DB 的是 common.Migrate，而它只「新增欄位」——既有的列一行都不改，
# 舊資料在新欄位上是 NULL，重複執行也沒有影響。
#
#   bash reload_web.sh
#
# 網址不變、tunnel 不動。重啟期間約 1–2 秒無法連線；作答頁的自動儲存會自己重試，
# 已填內容留在瀏覽器裡不會掉。
set -euo pipefail
cd "$(dirname "$0")"
PORT=8080

# shellcheck disable=SC1091
source env.prod.sh
mkdir -p logs

echo "--- 1/4 備份資料庫（線上快照，不影響作答）---"
.venv/bin/python backup_db.py

echo
echo "--- 2/4 檢查新版程式能不能載入（載入失敗就不重啟）---"
# 先在另一個行程 import，語法錯或 import 錯會在這裡擋下來，而不是把服務弄掛。
# 另外把「所有模板實際 url_for 到的 endpoint」跟路由表對一次——MyStats 那次的 500
# 正是模板引用了程式裡不存在的 endpoint，而那種錯只有在有人打開該頁時才會爆。
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
    print(f'  ✗ 模板 {\",\".join(files)} 用了不存在的 endpoint: {e}', file=sys.stderr)
if floor:
    print('  ✗ 少了路由: ' + ', '.join(sorted(floor)), file=sys.stderr)
print(f'  模板引用的 {len(used)} 個 endpoint 全部存在')
raise SystemExit(1 if (bad or floor) else 0)
"
echo "程式與路由 ✓"

echo
echo "--- 2.5/4 補資料庫欄位（只新增欄位，既有資料不動）---"
# ALTER TABLE ... ADD COLUMN 而已：不重選校準題、不重算分配、不改任何一列答案。
.venv/bin/python -c "
import common
conn = common.Connect()
added = common.Migrate(conn)
print('  新增欄位:', ', '.join(added) if added else '（已是最新）')
conn.close()
"

echo
echo "--- 3/4 重啟 gunicorn（只動本實例）---"
bash run.sh stop-web || true
sleep 1
bash run.sh start-web

for _ in $(seq 1 20); do
  sleep 1
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
done

echo
echo "--- 4/4 驗證新 worker 真的在跑新版 ---"
gpid=$(bash run.sh pid) || { echo "✗ gunicorn 沒起來（tail logs/gunicorn.log）"; exit 1; }
tr '\0' '\n' < "/proc/${gpid}/environ" | grep '^ESG_PID_ALLOWLIST=.' >/dev/null \
  && echo "白名單載入 ✓" || { echo "白名單沒載入 ✗（tail logs/gunicorn.log）"; exit 1; }

# healthz 只證明有東西在聽 port；未登入的 /mystats 回 302 是在 render 之前就返回，
# 什麼模板都沒跑到，證明不了任何事。這裡用測試帳號帶 cookie 真的登入，抓「登入後
# 才會出現的頁面」，檢查 base.html 整頁 render 完成（底部頁尾出現）且 url_for 沒爆。
# -w 4，所以打 12 次才有機會每個 worker 都被輪到。
jar=$(mktemp); trap 'rm -f "$jar" logs/check.html' EXIT
test_pid=${ESG_TEST_PIDS%%,*}
curl -s -c "$jar" -o /dev/null "http://127.0.0.1:${PORT}/?pid=${test_pid:-self-check}"
fail=0 consented=0
for _ in $(seq 1 12); do
  code=$(curl -s -b "$jar" -c "$jar" -L -o logs/check.html \
              -w '%{http_code}' "http://127.0.0.1:${PORT}/instructions")
  [ "$code" = "200" ] || { echo "  /instructions -> HTTP ${code}"; fail=1; continue; }
  # 頁尾只在 base.html 完整 render 到最後才會出現：中途 BuildError 會變成 500。
  grep '國立臺灣大學' logs/check.html >/dev/null \
    || { echo "  /instructions 回 200 但 base.html 沒 render 完"; fail=1; }
  grep 'href="/mystats"' logs/check.html >/dev/null && consented=1
done
[ "$fail" = 0 ] && echo "每個 worker 都能 render 登入後的頁面 ✓" \
                || { echo "✗ 有 worker render 失敗（tail logs/gunicorn.log）"; exit 1; }
[ "$consented" = 1 ] \
  && echo "header 的 url_for('MyStats') 解析正常 ✓" \
  || echo "提醒：測試帳號還沒同意過，所以沒驗到 header 的統計連結（用測試帳號按一次同意就能驗到）"

echo
echo "完成。網址不變，通道沒有重開。"
echo "接著請人工確認一題的「開啟報告書節錄 PDF」與「完整報告書」連結。"
