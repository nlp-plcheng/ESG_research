#!/usr/bin/env bash
# Publish the current tunnel address to the fixed link page: a one-file
# redirect page (index.html) in a GitHub Pages repository cloned at
# $ESG_LINK_REPO_DIR. Visitors are forwarded to the tunnel with the query
# string intact, so https://<you>.github.io/<repo>/?pid=... never changes.
#
# The repository must be dedicated to this page. Nothing but index.html (plus
# the usual README.md / .nojekyll / CNAME / .gitignore / LICENSE) may be
# tracked, nothing else may be staged, no unpushed commits by anyone else may
# sit on the branch, and no unpushed commit may contain any other file --
# otherwise the script refuses, so a stray file can never be published by
# accident. Only index.html is ever committed; history is never rewritten.
#
#   bash publish_link.sh https://xxxx.trycloudflare.com
#
# Exit codes: 0 the remote now points at the address (recorded in
# logs/published_url.txt); 3 not configured; 2 refused (foreign content in
# the repo); 4 remote diverged; 5 fetch/push failed; 1 other error.
set -euo pipefail
cd "$(dirname "$0")"
here=$(pwd)
mkdir -p logs
export GIT_TERMINAL_PROMPT=0   # never hang on a credential prompt

url=${1:?usage: publish_link.sh <tunnel-url>}
url=${url%/}
case "$url" in https://*) ;; *) echo "not an https URL: $url"; exit 1 ;; esac

# shellcheck disable=SC1091
[ -f env.prod.sh ] && source env.prod.sh
repo=${ESG_LINK_REPO_DIR:-}
if [ -z "$repo" ]; then
  echo "ESG_LINK_REPO_DIR is not set: no fixed link page to update"
  exit 3
fi
[ -d "$repo/.git" ] || { echo "$repo is not a git clone (git clone your Pages repo there first)"; exit 1; }
cd "$repo"
git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1 \
  || { echo "the clone has no upstream branch (run: git push -u origin main, once, by hand)"; exit 1; }
# Fresh view of the remote first, so "unpushed" below means exactly that.
fetched=1
git fetch -q origin || { fetched=0; echo "fetch failed (network? credentials?); checking against the last known remote state"; }

# --- refuse anything that could publish more than the redirect page ---------
allowed='^(index\.html|README\.md|\.nojekyll|CNAME|\.gitignore|LICENSE)$'
extra=$(git ls-files | grep -vE "$allowed" || true)
if [ -n "$extra" ]; then
  echo "refusing: the Pages repo tracks files other than the redirect page:"
  echo "$extra"
  echo "use a repository dedicated to this page (or remove those files from it)"
  exit 2
fi
if ! git diff --cached --quiet -- . ':(exclude)index.html'; then
  echo "refusing: other files are staged in the Pages repo (unstage them there first):"
  git diff --cached --name-only -- . ':(exclude)index.html'
  exit 2
fi
foreign=$(git log --format='%h %ae %s' '@{u}..HEAD' | grep -v ' esg-tunnel@localhost ' || true)
if [ -n "$foreign" ]; then
  echo "refusing: unpushed commits not made by this script sit on the branch:"
  echo "$foreign"
  exit 2
fi
# Every snapshot about to be pushed, not just the latest one: a forbidden file
# that was committed and deleted again would still travel in the history.
history=$(for c in $(git rev-list '@{u}..HEAD'); do git ls-tree -r --name-only "$c"; done \
          | sort -u | grep -vE "$allowed" || true)
if [ -n "$history" ]; then
  echo "refusing: the unpushed history contains files other than the redirect page:"
  echo "$history"
  echo "this script never rewrites history; drop those commits by hand in $repo" \
       "(e.g. git reset --hard '@{u}') and retry"
  exit 2
fi

# --- write the page, commit only that file --------------------------------
cat > index.html <<EOF
<!doctype html>
<meta charset="utf-8">
<meta name="robots" content="noindex">
<title>ESG commitment verification</title>
<p style="font-family:sans-serif">Taking you to the annotation platform…
   <a id="go" href="${url}/">Click here if you are not redirected automatically</a></p>
<script>
  // Forward to the platform, keeping the personal code (?pid=...) intact.
  var target = "${url}/" + location.search + location.hash;
  // ?go=/some/path (percent-encoded, may carry #page=N) forwards to that
  // path on the platform instead of the front page, so a deep link survives
  // a change of tunnel address. Only a plain absolute path is accepted.
  var go = new URLSearchParams(location.search).get("go");
  if (go && /^\\/(?![\\/\\\\])/.test(go)) target = "${url}" + go;
  document.getElementById("go").href = target;
  location.replace(target);
</script>
EOF
git add -- index.html
if ! git diff --cached --quiet -- index.html; then
  git -c user.name=esg-tunnel -c user.email=esg-tunnel@localhost \
      commit -q --only -m "tunnel address $(date '+%F %T')" -- index.html
fi

# --- push whatever is still unpushed (also after an earlier failed push) ----
[ "$fetched" = 1 ] || { echo "cannot push without a successful fetch; will be retried"; exit 5; }
if [ -n "$(git rev-list 'HEAD..@{u}')" ]; then
  # The remote moved (someone pushed by hand): replay our commit on top.
  git rebase -q '@{u}' 2>/dev/null || { git rebase --abort 2>/dev/null || true
                                        echo "remote diverged; resolve in $repo by hand"; exit 4; }
fi
if [ -n "$(git rev-list '@{u}..HEAD')" ]; then
  git push -q || { echo "push failed (network? credentials?); will be retried"; exit 5; }
fi
[ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] \
  || { echo "remote still differs from local after push; will be retried"; exit 5; }
grep -qF "\"${url}/\"" index.html || { echo "index.html does not contain the address"; exit 1; }

printf '%s\n' "$url" > "$here/logs/published_url.txt"
echo "published: fixed link page -> $url"
