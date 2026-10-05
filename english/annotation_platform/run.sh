#!/usr/bin/env bash
# One-command operation of the platform. Usage: bash run.sh <subcommand>
#
#   setup      create the venv, install packages, download cloudflared (once)
#   test       test profile: ingest + run the server in the foreground (Ctrl+C stops)
#   wipe       delete the test DB (production data untouched)
#   prod-init  create env.prod.sh (secrets filled in) + ingest into the production DB
#   serve      start the production service in the background: gunicorn + public tunnel
#              (tunnel_loop.sh: restarts the tunnel whenever it dies)
#   tunnel     restart only the public tunnel; the new address is published to the
#              fixed link page when one is configured
#   url        print the public address, the fixed link, the Prolific link, the admin link
#   links      print every personal link (only the first ESG_N_ANNOTATORS when set) + test accounts
#   status     is the service alive?
#   stop       stop the production service
#   export     write the three CSVs to exports/
#   stop-web / start-web / pid   gunicorn only (used by restart_web.sh and launch.sh)
#
# Order: setup -> test -> wipe -> prod-init -> (edit env.prod.sh) -> serve -> links
#
# Port / tmux session: export ESG_PORT (default 8081) and ESG_SESSION (default
# esg-en) in your shell before any of these; they are not read from env.prod.sh.
# The defaults are distinct from the Chinese edition's, and stop/restart only
# ever signal processes and tmux sessions verified to be this instance's, so
# several copies can run on one machine.

set -euo pipefail
cd "$(dirname "$0")"

PORT=${ESG_PORT:-8081}
SESSION=${ESG_SESSION:-esg-en}

Die() { echo "error: $*" >&2; exit 1; }

Activate() {
  [ -d .venv ] || Die "not set up yet -- run: bash run.sh setup"
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
    ForeignSession && Die "tmux session '${SESSION}' belongs to another copy of the platform (its panes run from a different folder) -- export ESG_SESSION=<another name> for this instance"
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
    tmux send-keys -t "$SESSION:tunnel" "cd '$here' && ESG_PORT=${PORT} bash tunnel_loop.sh" C-m
  else
    ESG_PORT=${PORT} nohup bash tunnel_loop.sh >> logs/tunnel_loop.log 2>&1 &
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
    echo "downloading cloudflared..."
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
    chmod +x cloudflared
  fi
  echo "setup done. next: bash run.sh test"
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
  echo "Open in a browser on this machine:"
  echo "  annotate  http://127.0.0.1:${PORT}/?pid=test01"
  echo "  admin     http://127.0.0.1:${PORT}/admin?key=testadmin"
  echo "On a remote machine, forward the port first (from your laptop):"
  echo "  ssh -N -L ${PORT}:localhost:${PORT} <user>@<server>"
  echo "Ctrl+C stops the server; bash run.sh wipe deletes the test data"
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
    echo "created env.prod.sh (SECRET_KEY / ADMIN_KEY filled in)"
  else
    echo "env.prod.sh exists, keeping it"
  fi
  # shellcheck disable=SC1091
  source env.prod.sh
  Ingest
  echo
  echo "next: edit env.prod.sh (participant ids, team size), then: bash run.sh serve"
}

Serve() {
  Activate
  [ -f env.prod.sh ] || Die "env.prod.sh missing -- run: bash run.sh prod-init"
  [ -x cloudflared ] || Die "cloudflared missing -- run: bash run.sh setup"
  mkdir -p logs

  Stop >/dev/null 2>&1 || true
  if PortBusy; then
    echo "port ${PORT} is in use by a process that is not this instance's:" >&2
    ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
    Die "stop that process first (by hand), or export ESG_PORT=<other port>, then serve again"
  fi
  # tunnel.log is append-mode; truncate it so `url` can never grep a stale
  # address from a previous run (dead URLs show Cloudflare error 1033).
  : > logs/tunnel.log
  StartWeb
  StartTunnel
  if command -v tmux >/dev/null 2>&1; then
    echo "started in tmux session '${SESSION}' (tmux attach -t ${SESSION} shows the logs)"
  else
    echo "started with nohup (logs under logs/)"
  fi

  echo "waiting for the service and the tunnel address (up to 30 s)..."
  for _ in $(seq 1 30); do
    sleep 1
    if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && Url >/dev/null 2>&1; then
      Url
      return 0
    fi
  done
  echo "not ready yet: run bash run.sh url in a few seconds; if it never comes up, check logs/gunicorn.log and logs/tunnel.log"
}

Tunnel() {
  # Restart only the public tunnel; gunicorn keeps serving. A quick tunnel
  # comes back with a NEW address -- the loop writes it to the fixed link
  # page when one is configured, and the links are printed again here.
  [ -f env.prod.sh ] || Die "env.prod.sh missing"
  mkdir -p logs
  StopTunnel || true
  sleep 1
  if command -v tmux >/dev/null 2>&1 && ForeignSession; then
    Die "tmux session '${SESSION}' belongs to another copy of the platform -- export ESG_SESSION=<another name> for this instance"
  fi
  local stray; stray=$(Foreign "$TUNNEL_RE" | tr '\n' ' ')
  [ -z "$stray" ] || Die "a tunnel to port ${PORT} is running that could not be verified as this instance's (pid ${stray}); not starting a second one -- check it with: ls -l /proc/<pid>/cwd"
  StartTunnel
  echo "waiting for the new public address (up to 30 s)..."
  for _ in $(seq 1 30); do
    sleep 1
    [ -s logs/tunnel_url.txt ] && { Links; return 0; }
  done
  echo "no address yet: run bash run.sh links in a moment (log: logs/tunnel.log)"
}

Url() {
  [ -f logs/tunnel.log ] || { echo "logs/tunnel.log not found -- is the service running? (bash run.sh serve)" >&2; return 1; }
  local base
  base=$(TunnelBase) || true
  [ -n "${base:-}" ] || { echo "the tunnel address has not appeared yet, try again shortly" >&2; return 1; }
  local admin_key="" page=""
  # shellcheck disable=SC1091
  [ -f env.prod.sh ] && admin_key=$(source env.prod.sh >/dev/null 2>&1; echo "${ESG_ADMIN_KEY:-}") \
                     && page=$(source env.prod.sh >/dev/null 2>&1; echo "${ESG_LINK_PAGE_URL:-}")
  # Everything handed to participants goes through the fixed entry when there
  # is one (the redirect keeps the query string); admin/health stay direct.
  local entry="${base}/"
  echo "public address (current tunnel)   ${base}"
  if [ -n "$page" ]; then
    entry="${page%/}/"
    echo "fixed link (annotators / Prolific) ${entry}   -> forwards to the address above; survives tunnel restarts"
  fi
  echo "Prolific study URL (paste into the study settings):"
  echo "  ${entry}?PROLIFIC_PID={{%PROLIFIC_PID%}}&STUDY_ID={{%STUDY_ID%}}&SESSION_ID={{%SESSION_ID%}}"
  echo "admin        ${base}/admin?key=${admin_key}   (direct; changes with the address)"
  echo "health       ${base}/healthz"
}

Links() {
  [ -f env.prod.sh ] || Die "env.prod.sh missing"
  # shellcheck disable=SC1091
  source env.prod.sh
  local base
  base=$(TunnelBase) || true
  [ -n "${base:-}" ] || Die "the tunnel address has not appeared yet -- is the service running? (bash run.sh serve)"
  # Annotators get the fixed link page when there is one (it forwards ?pid=
  # to the current tunnel address); the admin link always goes direct.
  local entry="${base}/"
  if [ -n "${ESG_LINK_PAGE_URL:-}" ]; then
    entry="${ESG_LINK_PAGE_URL%/}/"
    echo "fixed link page: ${entry}  ->  currently forwards to ${base}"
  else
    echo "note: a quick tunnel changes its address on every restart; set ESG_LINK_PAGE_URL / ESG_LINK_REPO_DIR for a fixed link (see README)"
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
      echo "warning: ESG_N_ANNOTATORS=${n} but the allowlist has only ${#real[@]} non-test ids (bash add_pids.sh adds more)" >&2
    fi
    real=("${real[@]:0:$n}")
    echo "=== annotator links: ESG_N_ANNOTATORS=${n}, only these ${#real[@]} ids can enter (one per person, do not swap) ==="
  else
    echo "=== annotator links (one per person, do not swap) ==="
  fi
  for p in "${real[@]}"; do echo "  ${entry}?pid=${p}"; done
  echo
  echo "=== test accounts (always admitted, see every item, hold no slot, excluded from the dataset) ==="
  for t in "${tests[@]}"; do [ -n "$t" ] && echo "  ${entry}?pid=${t}"; done
  echo
  echo "admin: ${base}/admin?key=${ESG_ADMIN_KEY}"
}

Status() {
  if curl -sf "http://127.0.0.1:${PORT}/healthz" 2>/dev/null; then echo "  <- gunicorn OK"
  else echo "gunicorn is not responding on port ${PORT}"; fi
  if command -v tmux >/dev/null 2>&1; then
    if OwnSession; then echo "tmux session '${SESSION}' exists"
    elif HasSession; then
      echo "tmux session '${SESSION}' exists but belongs to another copy of the platform (different folder)"
    fi
  fi
  if [ -n "$(Own "$TUNNEL_RE")" ]; then echo "tunnel process running"
  else echo "tunnel process NOT running (bash run.sh tunnel)"; fi
  Url || true
}

Stop() {
  local stopped=0
  if command -v tmux >/dev/null 2>&1; then
    if OwnSession; then tmux kill-session -t "$SESSION"; stopped=1
    elif HasSession; then
      echo "note: tmux session '${SESSION}' belongs to another copy of the platform; left alone" >&2
    fi
  fi
  StopWeb && stopped=1
  StopTunnel && stopped=1
  if PortBusy; then
    echo "warning: port ${PORT} is still held by a process that is not this instance's:" >&2
    ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
    return 1
  fi
  [ "$stopped" = 1 ] && echo "stopped, port ${PORT} released" || echo "nothing was running"
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
  start-web) [ -d .venv ] || Die "not set up yet -- run: bash run.sh setup"
             [ -f env.prod.sh ] || Die "env.prod.sh missing"
             mkdir -p logs; StartWeb ;;
  pid)       GunicornPid ;;
  *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
