#!/bin/bash
# In-app update. Run detached by POST /api/update, never in the server's own
# process: the last step restarts that server, which would kill an update
# running inside it halfway through.
#
# Writes progress to ~/Library/Logs/AIVideoStudio-update.log, which the app
# reads and shows while it works.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The log lives OUTSIDE the repo on purpose. Inside it, `git stash -u` sweeps it
# away mid-run: the script keeps writing to a deleted file and the app watches a
# log that stopped growing. (Found the hard way in the update rehearsal.)
LOG="$HOME/Library/Logs/AIVideoStudio-update.log"
# Which commit this run was aiming at, and how it ended. The auto-updater reads
# it so a release that cannot build here is attempted ONCE — without this it
# rolls back, stays "behind", and is retried every half hour forever.
STATE="$HOME/Library/Logs/AIVideoStudio-update-state.json"
mkdir -p "$(dirname "$LOG")"
record() {  # record <ok|failed>
  target="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('target',''))" "$STATE" 2>/dev/null || true)"
  printf '{"target": "%s", "result": "%s", "at": %s}\n' "$target" "$1" "$(date +%s)" > "$STATE"
}
cd "$ROOT"

# Ask launchd which service actually serves THIS folder instead of assuming the
# installer's label — an older install may be running under a different name.
LABEL="${VSTUDIO_LABEL:-}"
if [ -z "$LABEL" ]; then
  for p in "$HOME"/Library/LaunchAgents/*.plist; do
    [ -f "$p" ] || continue
    if grep -q "$ROOT/" "$p" 2>/dev/null; then LABEL="$(basename "$p" .plist)"; break; fi
  done
fi
LABEL="${LABEL:-com.aivideostudio.server}"

exec >"$LOG" 2>&1
say() { printf "%s\n" "$*"; }

say "STEP fetching the new version"
# Local edits must never block an update, and must never be thrown away either:
# stash them, update, put them back. Tracked files only — sweeping up untracked
# ones with -u is a trap: the update
# regenerates uv.lock, package-lock.json and friends, so putting the stash back
# collides with the files the update itself just wrote — every single time.
STASHED=0
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  git stash push -m "auto-stash before update" >/dev/null 2>&1 && STASHED=1
  [ "$STASHED" = "1" ] && say "  (set your local changes aside)"
fi

# Putting them back can collide with the new version. A conflicted `stash pop`
# leaves <<<<<<< markers inside the app's own source, which breaks it far worse
# than losing the edit would. So: on collision, keep the clean new version and
# leave the edit parked in the stash, where it can still be recovered.
restore() {
  [ "$STASHED" = "1" ] || return 0
  if git stash pop >/dev/null 2>&1; then
    STASHED=0
    return 0
  fi
  # Undo ONLY the files that actually collided. A blanket `reset --hard` would
  # also throw away whatever the build just produced. The stash is left intact,
  # so nothing the student wrote is gone.
  CONF="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
  if [ -n "$CONF" ]; then
    printf "%s\n" "$CONF" | tr '\n' '\0' | xargs -0 git checkout --force HEAD -- 2>/dev/null
    git reset -q >/dev/null 2>&1
  else
    git reset --hard HEAD >/dev/null 2>&1
  fi
  say "  (your edits clash with the new version — they are parked in 'git stash list')"
}

WAS="$(git rev-parse HEAD 2>/dev/null || true)"
PULL="$(git pull --ff-only 2>&1)" || {
  say "$PULL"
  case "$PULL" in
    *"untracked working tree file"*|*"untracked files"*)
      say "ERROR a file listed above exists here but is not part of the app, and the"
      say "      new version wants to add its own. Rename yours and update again." ;;
    *) say "ERROR could not update: this copy has diverged from the published one."
       say "      Nothing was changed. Ask for help before forcing anything." ;;
  esac
  restore
  record failed
  say "DONE failed"
  exit 1
}
say "$PULL"
NEW="$(git log --oneline -1)"
say "  now at: $NEW"

say "STEP python dependencies"
uv sync --quiet 2>&1 || say "  (uv sync reported a problem — continuing)"

say "STEP rebuilding the interface"
( cd web && npm install --silent 2>&1 && npm run --silent build 2>&1 ) || {
  # Go back to the version that was working. Half-updated is worse than old:
  # new server code against an old interface is a mismatch nobody can debug.
  [ -n "$WAS" ] && git reset --hard "$WAS" >/dev/null 2>&1
  ( cd web && npm run --silent build >/dev/null 2>&1 ) || true
  say "ERROR the new version failed to build. Put back the one that was working —"
  say "      nothing changed. Try again later, or run ./install.sh to repair."
  restore
  record failed
  say "DONE failed"
  exit 1
}

restore

record ok
say "STEP restarting"
# launchd brings it straight back (KeepAlive). The browser is polling
# /api/version and reloads itself as soon as the new build answers.
say "DONE ok"
sleep 1
if ! launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # No launchd service under that name. Drop the listener and let whatever
  # supervises it bring it back.
  #
  # -sTCP:LISTEN is not optional: without it lsof also lists every process
  # CONNECTED to the port — the browser polling this very update — and the
  # kill lands on the wrong process. That is how the first rehearsal ended
  # with the server still running and something else dead.
  PIDS="$(/usr/sbin/lsof -ti tcp:"${VSTUDIO_PORT:-3030}" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$PIDS" ]; then
    # SIGTERM hangs under a keep-alive supervisor; go straight to -9
    printf "%s\n" "$PIDS" | xargs kill -9 2>/dev/null || true
  fi
fi
