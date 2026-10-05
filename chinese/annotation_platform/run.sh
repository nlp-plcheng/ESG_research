#!/usr/bin/env bash
# 平台一鍵操作。用法：bash run.sh <子指令>
#
#   setup      建 venv、裝套件、下載 cloudflared（跑一次就好）
#   test       測試 profile：倒資料 + 前景起 server（Ctrl+C 停）
#   wipe       洗掉測試 DB（不碰正式資料）
#   prod-init  產生 env.prod.sh（自動填密鑰）+ 倒資料進正式 DB
#   serve      背景起正式服務：gunicorn + 對外通道（tunnel_loop.sh：掛了自動重開）
#   tunnel     只重開對外通道，gunicorn 不動；新網址會自動寫進固定連結頁（若有設定）
#   url        印出對外網址、固定連結、Prolific 研究連結、後台連結
#   links      印出每人專屬連結（ESG_N_ANNOTATORS>0 時只印前 N 條）＋測試帳號連結
#   status     檢查服務活著沒
#   stop       停掉正式服務
#   export     匯出三份 CSV 到 exports/
#   stop-web / start-web / pid   只動 gunicorn（restart_web.sh、launch.sh 在用）
#
# 順序：setup -> test -> wipe -> prod-init -> (填 env.prod.sh) -> serve -> links
#
# port 固定 8080（英文版預設 8081）；tmux session 名稱預設 esg，可在 shell 先
# export ESG_SESSION=<名稱> 覆寫。stop / restart 只會對「經核對確實屬於本實例」的
# 程序與 tmux session 送訊號（工作目錄＋port），同一台機器上的另一個平台不會被誤殺。

set -euo pipefail
cd "$(dirname "$0")"

PORT=8080
SESSION=${ESG_SESSION:-esg}

Die() { echo "錯誤：$*" >&2; exit 1; }

Activate() {
  [ -d .venv ] || Die "還沒 setup，先跑：bash run.sh setup"
  # shellcheck disable=SC1091
  source .venv/bin/activate
}

# True while something is listening on PORT (pure bash, no ss/netstat needed).
PortBusy() { (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; }

WaitPortFree() {
  for _ in $(seq 1 15); do
    PortBusy || return 0
    sleep 1
  done
  return 1
}

GUNICORN="gunicorn -w 4 -b 127.0.0.1:${PORT} --pid logs/gunicorn.pid app:app"

# ---- own-process / own-session discovery ------------------------------------
# A process is signalled only if it is verifiably this instance's: its working
# directory is THIS folder and its command line matches. A pid file is never
# trusted on its own -- a stale one may name a pid the kernel has since handed
# to something else -- and another platform instance on the same machine (even
# one bound to the same port) is never touched. The same rule applies to the
# tmux session: it is ours only if one of its panes runs from this folder.
# (No `grep -q` on a pipe anywhere here: -q exits early, the writer gets
# SIGPIPE and `pipefail` turns that into a false failure.)
HERE=$(pwd -P)
GUNICORN_RE="gunicorn .*-b 127[.]0[.]0[.]1:${PORT} "
LOOP_RE='bash .*tunnel_loop[.]sh'
TUNNEL_RE="cloudflared tunnel .*localhost:${PORT}"'( |$)'

Own() {  # Own <ERE>: pids of this instance's processes whose command line matches
  local pid
  for pid in $(pgrep -f "$1" 2>/dev/null); do
    [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$HERE" ] && echo "$pid"
  done
  return 0
}

KillOwn() {  # KillOwn <ERE> <SIGNAL>: true if anything was signalled
  local pid any=1
  for pid in $(Own "$1"); do kill "-$2" "$pid" 2>/dev/null && any=0; done
  return $any
}

Foreign() {  # Foreign <ERE>: matching pids that are NOT verifiably ours (listed, never signalled)
  local pid own
  own=" $(Own "$1" | tr '\n' ' ') "
  for pid in $(pgrep -f "$1" 2>/dev/null); do
    case "$own" in *" $pid "*) ;; *) echo "$pid" ;; esac
  done
  return 0
}

GunicornPid() {  # this instance's gunicorn master: the pid file, verified against Own()
  local pid own
  pid=$(cat logs/gunicorn.pid 2>/dev/null || true)
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  own=" $(Own "$GUNICORN_RE" | tr '\n' ' ') "
  case "$own" in *" $pid "*) echo "$pid" ;; *) return 1 ;; esac
}

HasSession() {  # exact name only: tmux's own -t matching also accepts prefixes (esg -> esg-en)
  tmux list-sessions -F '#S' 2>/dev/null | grep -x "$SESSION" >/dev/null
}

OwnSession() {  # the tmux session exists and one of its panes runs from this folder
  HasSession || return 1
  local p
  while IFS= read -r p; do
    [ "$p" = "$HERE" ] && return 0
  done < <(tmux list-panes -s -t "$SESSION" -F '#{pane_current_path}' 2>/dev/null)
  return 1
}

ForeignSession() {  # a same-named session that belongs to another copy of the platform
  HasSession && ! OwnSession
}

# Current public address: written by tunnel_loop.sh; the log is the fallback.
TunnelBase() {
  local base=""
  [ -s logs/tunnel_url.txt ] && base=$(head -1 logs/tunnel_url.txt)
  [ -n "$base" ] || base=$(grep -oE 'https://[a-z0-9.-]+\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | tail -1) || true
  [ -n "$base" ] && echo "$base"
}

# gunicorn in the tmux web window (or with nohup).
StartWeb() {
  local here; here=$(pwd)
  if command -v tmux >/dev/null 2>&1; then
    ForeignSession && Die "tmux session「${SESSION}」屬於另一份平台副本（它的視窗在別的資料夾跑）—— 本實例請先 export ESG_SESSION=<別的名稱>"
    OwnSession || tmux new-session -d -s "$SESSION" -n web
    tmux list-windows -t "$SESSION" -F '#W' | grep -x web >/dev/null || tmux new-window -t "$SESSION" -n web
    tmux send-keys -t "$SESSION:web" \
      "cd '$here' && source .venv/bin/activate && source env.prod.sh && ${GUNICORN} 2>&1 | tee -a logs/gunicorn.log" C-m
  else
    nohup bash -c "source .venv/bin/activate && source env.prod.sh && exec ${GUNICORN}" \
      >> logs/gunicorn.log 2>&1 &
  fi
}

StopWeb() {  # true if something was stopped
  # gunicorn treats the SIGHUP it gets when its tmux pane dies as "reload", not
  # "quit", so TERM the master explicitly (graceful worker shutdown), wait for
  # the port to be released, KILL only as a last resort. Own processes only.
  local stopped=1
  KillOwn "$GUNICORN_RE" TERM && stopped=0
  if ! WaitPortFree; then
    KillOwn "$GUNICORN_RE" KILL || true
    sleep 1
  fi
  rm -f logs/gunicorn.pid
  return $stopped
}

# Start the tunnel loop in the tmux window (or with nohup); gunicorn untouched.
StartTunnel() {
  local here; here=$(pwd)
  rm -f logs/tunnel_url.txt
  if command -v tmux >/dev/null 2>&1 && OwnSession; then
    tmux kill-window -t "$SESSION:tunnel" 2>/dev/null || true
    tmux new-window -t "$SESSION" -n tunnel
    tmux send-keys -t "$SESSION:tunnel" "cd '$here' && bash tunnel_loop.sh" C-m
  else
    nohup bash tunnel_loop.sh >> logs/tunnel_loop.log 2>&1 &
  fi
}

StopTunnel() {  # true if something was stopped
  local any=1 pid
  KillOwn "$LOOP_RE" TERM && any=0      # the loop first, or it would just restart cloudflared
  KillOwn "$TUNNEL_RE" TERM && any=0
  # A publish still running (the loop's trap takes it down too): its whole
  # process group, git included.
  for pid in $(Own 'bash .*publish_link[.]sh'); do
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  return $any
}

Setup() {
  if [ ! -d .venv ]; then python3 -m venv .venv; fi
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  if [ ! -x cloudflared ]; then
    echo "下載 cloudflared..."
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
    chmod +x cloudflared
  fi
  echo "setup 完成。下一步：bash run.sh test"
}

Ingest() {
  # ingest.py also marks the calibration set (top-N priority multi-year items)
  # and, when ESG_N_ANNOTATORS > 0, pre-allocates items to annotator slots.
  python ingest.py
}

Test() {
  Activate
  # shellcheck disable=SC1091
  source env.test.sh
  Ingest
  echo
  echo "================================================================"
  echo "在你自己的筆電開 tunnel（帳號、主機換成你的）："
  echo "  ssh -N -L ${PORT}:localhost:${PORT} <user>@<server>"
  echo "然後本機瀏覽器開："
  echo "  作答  http://127.0.0.1:${PORT}/?pid=test01"
  echo "  後台  http://127.0.0.1:${PORT}/admin?key=testadmin"
  echo "測完 Ctrl+C 停掉，再跑 bash run.sh wipe 洗資料"
  echo "================================================================"
  echo
  python app.py
}

Wipe() {
  Activate
  # shellcheck disable=SC1091
  source env.test.sh
  python reset.py --all --yes
}

ProdInit() {
  Activate
  if [ ! -f env.prod.sh ]; then
    SK=$(python -c 'import secrets; print(secrets.token_hex(32))')
    AK=$(python -c 'import secrets; print(secrets.token_hex(16))')
    sed -e "s|^export ESG_SECRET_KEY=.*|export ESG_SECRET_KEY=${SK}|" \
        -e "s|^export ESG_ADMIN_KEY=.*|export ESG_ADMIN_KEY=${AK}|" \
        env.prod.sh.example > env.prod.sh
    echo "已產生 env.prod.sh（SECRET_KEY / ADMIN_KEY 已自動填好）"
  else
    echo "env.prod.sh 已存在，沿用"
  fi
  # shellcheck disable=SC1091
  source env.prod.sh
  Ingest
  echo
  if [ -z "${ESG_PROLIFIC_CODE:-}" ]; then
    echo "剩一件事：把 Prolific 後台的 completion code 填進 env.prod.sh 的 ESG_PROLIFIC_CODE"
    echo "填完再跑：bash run.sh serve"
  else
    echo "下一步：編輯 env.prod.sh（參與者代號、人數），然後：bash run.sh serve"
  fi
}

Serve() {
  Activate
  [ -f env.prod.sh ] || Die "env.prod.sh 不存在，先跑：bash run.sh prod-init"
  [ -x cloudflared ] || Die "cloudflared 不存在，先跑：bash run.sh setup"
  mkdir -p logs

  Stop >/dev/null 2>&1 || true
  if PortBusy; then
    echo "port ${PORT} 被「不屬於本實例」的程式佔用：" >&2
    ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
    Die "先手動把上面的程式關掉，再跑 serve"
  fi
  # tunnel.log is append-mode; truncate it so `url` can never grep a stale
  # address from a previous run (dead URLs show Cloudflare error 1033).
  : > logs/tunnel.log
  StartWeb
  StartTunnel
  if command -v tmux >/dev/null 2>&1; then
    echo "已在 tmux session「${SESSION}」啟動（tmux attach -t ${SESSION} 可看 log）"
  else
    echo "已用 nohup 啟動（log 在 logs/ 底下）"
  fi

  echo "等待服務與 tunnel 網址（最多 30 秒）..."
  for _ in $(seq 1 30); do
    sleep 1
    if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && Url >/dev/null 2>&1; then
      Url
      return 0
    fi
  done
  echo "還沒就緒。等幾秒後跑 bash run.sh url 再看；起不來就查 logs/gunicorn.log、logs/tunnel.log"
}

Tunnel() {
  # Restart only the public tunnel; gunicorn keeps serving. A quick tunnel
  # comes back with a NEW address -- the loop writes it to the fixed link
  # page when one is configured, and the links are printed again here.
  [ -f env.prod.sh ] || Die "env.prod.sh 不存在"
  mkdir -p logs
  StopTunnel || true
  sleep 1
  if command -v tmux >/dev/null 2>&1 && ForeignSession; then
    Die "tmux session「${SESSION}」屬於另一份平台副本 —— 本實例請先 export ESG_SESSION=<別的名稱>"
  fi
  local stray; stray=$(Foreign "$TUNNEL_RE" | tr '\n' ' ')
  [ -z "$stray" ] || Die "有一條通到 port ${PORT} 的通道在跑，但無法確認是本實例的（pid ${stray}）；不另開第二條。請先用 ls -l /proc/<pid>/cwd 看它是誰的"
  StartTunnel
  echo "等待新的對外網址（最多 30 秒）..."
  for _ in $(seq 1 30); do
    sleep 1
    [ -s logs/tunnel_url.txt ] && { Links; return 0; }
  done
  echo "還沒拿到網址：稍後跑 bash run.sh links 再看（log：logs/tunnel.log）"
}

Url() {
  [ -f logs/tunnel.log ] || { echo "找不到 logs/tunnel.log，服務起了嗎？（bash run.sh serve）" >&2; return 1; }
  local base
  base=$(TunnelBase) || true
  [ -n "${base:-}" ] || { echo "tunnel 網址還沒出現，稍等再試" >&2; return 1; }
  local admin_key="" page=""
  # shellcheck disable=SC1091
  [ -f env.prod.sh ] && admin_key=$(source env.prod.sh >/dev/null 2>&1; echo "${ESG_ADMIN_KEY:-}") \
                     && page=$(source env.prod.sh >/dev/null 2>&1; echo "${ESG_LINK_PAGE_URL:-}")
  # Everything handed to participants goes through the fixed entry when there
  # is one (the redirect keeps the query string); admin/health stay direct.
  local entry="${base}/"
  echo "對外網址（目前的通道）  ${base}"
  if [ -n "$page" ]; then
    entry="${page%/}/"
    echo "固定連結（給標註者／Prolific）  ${entry}   → 轉址到上面的網址，網址變了也不用換"
  fi
  echo "Prolific 研究連結（貼到 study 設定）："
  echo "  ${entry}?PROLIFIC_PID={{%PROLIFIC_PID%}}&STUDY_ID={{%STUDY_ID%}}&SESSION_ID={{%SESSION_ID%}}"
  echo "後台            ${base}/admin?key=${admin_key}   （直連；網址變了要重新拿）"
  echo "健康檢查        ${base}/healthz"
}

Links() {
  [ -f env.prod.sh ] || Die "env.prod.sh 不存在"
  # shellcheck disable=SC1091
  source env.prod.sh
  local base
  base=$(TunnelBase) || true
  [ -n "${base:-}" ] || Die "tunnel 網址還沒出現，服務起了嗎？（bash run.sh serve）"
  # Annotators get the fixed link page when there is one (it forwards ?pid=
  # to the current tunnel address); the admin link always goes direct.
  local entry="${base}/"
  if [ -n "${ESG_LINK_PAGE_URL:-}" ]; then
    entry="${ESG_LINK_PAGE_URL%/}/"
    echo "固定連結頁：${entry}  →  目前轉址到 ${base}"
  else
    echo "提醒：quick tunnel 每次重開網址就變；設定 ESG_LINK_PAGE_URL / ESG_LINK_REPO_DIR 可拿到固定連結（見 README）"
  fi
  local -a all tests real=()
  IFS=',' read -ra all <<< "${ESG_PID_ALLOWLIST:-}"
  IFS=',' read -ra tests <<< "${ESG_TEST_PIDS:-self-check}"
  local p t is_test
  for p in "${all[@]}"; do
    [ -n "$p" ] || continue
    is_test=0
    for t in "${tests[@]}"; do [ "$p" = "$t" ] && is_test=1; done
    [ "$is_test" = 1 ] || real+=("$p")
  done
  local n=${ESG_N_ANNOTATORS:-0}
  if [ "$n" -gt 0 ]; then
    if [ "${#real[@]}" -lt "$n" ]; then
      echo "警告：ESG_N_ANNOTATORS=${n} 但白名單只有 ${#real[@]} 個非測試 pid（bash add_pids.sh 可補）" >&2
    fi
    real=("${real[@]:0:$n}")
    echo "=== 標註者連結：ESG_N_ANNOTATORS=${n}，只有這 ${#real[@]} 個 pid 能進（一人一條，不要互換）==="
  else
    echo "=== 標註者連結（一人一條，不要互換）==="
  fi
  for p in "${real[@]}"; do echo "  ${entry}?pid=${p}"; done
  echo
  echo "=== 測試帳號（隨時可進、看全部題目、不佔名額、不進資料集）==="
  for t in "${tests[@]}"; do [ -n "$t" ] && echo "  ${entry}?pid=${t}"; done
  echo
  echo "後台：${base}/admin?key=${ESG_ADMIN_KEY}"
}

Status() {
  if curl -sf "http://127.0.0.1:${PORT}/healthz" 2>/dev/null; then echo "  <- gunicorn OK"
  else echo "gunicorn 沒回應"; fi
  if command -v tmux >/dev/null 2>&1; then
    if OwnSession; then echo "tmux session「${SESSION}」存在"
    elif HasSession; then
      echo "tmux session「${SESSION}」存在，但屬於另一份平台副本（別的資料夾）"
    fi
  fi
  if [ -n "$(Own "$TUNNEL_RE")" ]; then echo "通道程序在跑"
  else echo "通道程序沒在跑（bash run.sh tunnel）"; fi
  Url || true
}

Stop() {
  local stopped=0
  if command -v tmux >/dev/null 2>&1; then
    if OwnSession; then tmux kill-session -t "$SESSION"; stopped=1
    elif HasSession; then
      echo "提醒：tmux session「${SESSION}」屬於另一份平台副本，沒有動它" >&2
    fi
  fi
  StopWeb && stopped=1
  StopTunnel && stopped=1
  if PortBusy; then
    echo "警告：port ${PORT} 仍被「不屬於本實例」的程式佔用：" >&2
    ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
    return 1
  fi
  [ "$stopped" = 1 ] && echo "已停止，port ${PORT} 已釋放" || echo "沒有在跑的服務"
}

Export() {
  Activate
  # shellcheck disable=SC1091
  source env.prod.sh
  python export.py --out_dir exports
}

case "${1:-}" in
  setup)     Setup ;;
  test)      Test ;;
  wipe)      Wipe ;;
  prod-init) ProdInit ;;
  serve)     Serve ;;
  tunnel)    Tunnel ;;
  url)       Url ;;
  links)     Links ;;
  status)    Status ;;
  stop)      Stop ;;
  export)    Export ;;
  stop-web)  StopWeb ;;
  start-web) [ -d .venv ] || Die "還沒 setup，先跑：bash run.sh setup"
             [ -f env.prod.sh ] || Die "env.prod.sh 不存在"
             mkdir -p logs; StartWeb ;;
  pid)       GunicornPid ;;
  *) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
