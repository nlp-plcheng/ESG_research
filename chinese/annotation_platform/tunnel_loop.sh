#!/usr/bin/env bash
# Keep the public tunnel alive without root: run a Cloudflare quick tunnel,
# restart it whenever it exits (crash, network, self-update...), record the
# current public address in logs/tunnel_url.txt, and publish it to the fixed
# link page (publish_link.sh) so the address annotators hold keeps working
# even though a quick tunnel gets a new random hostname on every start.
#
# Two addresses are tracked separately: logs/tunnel_url.txt is what the tunnel
# reports (observed), logs/published_url.txt is what the fixed link page is
# known to point at (written by publish_link.sh on success only). A publisher
# loop beside the tunnel keeps trying until they match. No pid files: every
# helper is a child of this script, remembered in a variable and stopped by
# the trap -- nothing is ever signalled by a number read from a file. A
# publish that is still running when the loop is stopped goes down with it
# (own process group), so an old address can never be pushed after a restart.
#
#   bash tunnel_loop.sh          # normally started by run.sh serve / run.sh tunnel
set -u
cd "$(dirname "$0")"
mkdir -p logs
PORT=${ESG_PORT:-8080}
# shellcheck disable=SC1091
[ -f env.prod.sh ] && source env.prod.sh
PAGE=${ESG_LINK_PAGE_URL:-}
CONFIGURED=${ESG_LINK_REPO_DIR:-}

Log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a logs/tunnel.log; }
Observed()  { cat logs/tunnel_url.txt 2>/dev/null || true; }
Published() { cat logs/published_url.txt 2>/dev/null || true; }

# The publisher's current child (publish_link.sh or a sleep); see Publisher().
child=""

# Run a command as a background child and wait for it, so that a TERM aimed
# at the publisher is acted on at once (bash runs a trap only after the
# foreground command has finished). With --group the child gets its own
# process group (setsid), so git and everything else it starts can be taken
# down together with it.
Run() {
  local group=0
  if [ "$1" = "--group" ]; then group=1; shift; fi
  if [ "$group" = 1 ] && command -v setsid >/dev/null 2>&1; then setsid "$@" & else "$@" & fi
  child=$!
  wait "$child"
  local rc=$?
  child=""
  return $rc
}

# Poll the public page until it serves the new address: a push is not the
# same as GitHub Pages having deployed it (that can take minutes). Gives up
# early when the address has changed again meanwhile.
VerifyLive() {
  local url=$1 i
  [ -n "$PAGE" ] || return 0
  for i in $(seq 1 30); do
    if curl -sfL --max-time 20 "${PAGE%/}/" | grep -F "$url" >/dev/null; then
      Log "fixed link page is live: ${PAGE%/}/ -> $url"
      return 0
    fi
    [ "$(Observed)" = "$url" ] || return 0
    Run sleep 30
  done
  Log "fixed link page still not serving $url after 15 min -- check the Pages build (repo Settings -> Pages)"
}

# Runs beside the tunnel for the life of this script: whenever the observed
# address differs from the published one, publish it. A transient failure is
# retried after 60 s, a refusal (foreign content in the Pages repo) after 10
# min; "not configured" (exit 3) is reported once per address.
Publisher() {
  local url rc next=0 skip=""
  # Stopped (by the main script's trap, or by run.sh): take the publish that
  # may be running down with us -- its whole process group -- so a
  # half-finished publish of an OLD address can never land after a restart.
  trap '[ -n "$child" ] && { kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null; }; exit 0' TERM INT
  while true; do
    url=$(Observed)
    if [ -n "$url" ] && [ "$url" != "$(Published)" ] && [ "$url" != "$skip" ] \
       && [ "$(date +%s)" -ge "$next" ]; then
      Run --group bash publish_link.sh "$url" >> logs/publish.log 2>&1; rc=$?
      case $rc in
        0) Log "fixed link page -> $url (pushed; waiting for GitHub Pages to serve it)"
           VerifyLive "$url" ;;
        2) Log "publishing REFUSED: the Pages repo holds content other than the redirect page -- see logs/publish.log; retrying in 10 min"
           next=$(( $(date +%s) + 600 )) ;;
        3) Log "no fixed link page configured (ESG_LINK_REPO_DIR): the address changes on every restart, re-send links"
           skip=$url ;;
        *) Log "publishing failed (exit $rc); retrying in 60 s -- see logs/publish.log"
           next=$(( $(date +%s) + 60 )) ;;
      esac
    fi
    Run sleep 5
  done
}

# Reads cloudflared's output line by line: keeps the log and records every
# new public address (the publisher picks it up within seconds).
Watch() {
  local line url
  while IFS= read -r line; do
    printf '%s\n' "$line" | tee -a logs/tunnel.log
    url=$(printf '%s' "$line" | grep -oE 'https://[a-z0-9.-]+\.trycloudflare\.com' | head -1 || true)
    [ -n "$url" ] || continue
    if [ "$url" != "$(Observed)" ]; then
      printf '%s\n' "$url" > logs/tunnel_url.txt
      Log "tunnel URL: $url"
      [ -n "$CONFIGURED" ] || Log "no fixed link page configured (ESG_LINK_REPO_DIR): the address changes on every restart, re-send links"
    fi
  done
}

cf="" pub=""
Quit() {
  Log "tunnel loop stopping"
  [ -n "$cf" ] && kill "$cf" 2>/dev/null
  [ -n "$pub" ] && kill "$pub" 2>/dev/null
  exit 0
}
trap Quit TERM INT

if [ -n "$CONFIGURED" ]; then
  Publisher &
  pub=$!
fi

while true; do
  Log "starting cloudflared quick tunnel -> http://localhost:${PORT}"
  # --no-autoupdate: cloudflared's daily self-update shuts the process down
  # after updating itself, which would kill the tunnel.
  ./cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}" > >(Watch) 2>&1 &
  cf=$!
  wait "$cf"
  cf=""
  Log "cloudflared exited; restarting in 5 s"
  sleep 5
done
