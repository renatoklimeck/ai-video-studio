#!/bin/bash
# Run ONCE before publishing this repo to students. Audits and fixes the two
# things that make a private working copy unsafe to share:
#
#   1. Files under projects/ that were committed before .gitignore covered them.
#      A student cloning the repo would download YOUR footage, and worse: every
#      `git pull` update would then fight with THEIR project.json files.
#   2. Anything that looks like a credential.
#
# It only UNTRACKS (git rm --cached) — your local files are never deleted. It
# stages the change and stops; you review `git status` and commit yourself.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

bold "1. Files under projects/ that are tracked"
TRACKED="$(git ls-files projects/ || true)"
if [ -z "$TRACKED" ]; then
  ok "none — nothing of yours would ship"
else
  printf "%s\n" "$TRACKED" | sed 's/^/     /'
  SIZE=$(printf "%s\n" "$TRACKED" | tr '\n' '\0' | xargs -0 du -ch 2>/dev/null | tail -1 | cut -f1)
  warn "the above (${SIZE:-?}) would be cloned by every student"
  read -r -p "  Untrack them? Your local files stay put. [Y/n] " a </dev/tty || a="Y"
  case "${a:-Y}" in
    [Nn]*) warn "left as is — do NOT publish before dealing with this" ;;
    *) git rm -r --cached --quiet projects/
       # without this they come straight back on the next `git add -A`, and a
       # student's project.json would then collide with yours on every update
       grep -qx 'projects/' .gitignore 2>/dev/null || {
         printf '\n# every machine keeps its own projects — never shared\nprojects/\n' >> .gitignore
         git add .gitignore
         ok "added projects/ to .gitignore"
       }
       ok "untracked and staged (your files on disk are untouched)" ;;
  esac
fi

bold "2. Credentials in tracked files"
HITS="$(git grep -nIE '(sk-[A-Za-z0-9_-]{16,}|BEGIN [A-Z ]*PRIVATE KEY|xox[baprs]-)' \
        -- . ':!*.lock' ':!scripts/prepare-release.sh' 2>/dev/null || true)"
if [ -z "$HITS" ]; then ok "nothing that looks like a key"
else printf "%s\n" "$HITS" | sed 's/^/     /'; warn "review these before publishing"; fi

bold "3. Files the installer regenerates (must stay out of the repo)"
for p in .certs/ .venv/ node_modules/ web/dist/ server.log; do
  if git ls-files --error-unmatch "$p" >/dev/null 2>&1; then warn "$p IS tracked — add it to .gitignore"
  else ok "$p ignored"; fi
done

bold "4. What a student actually needs"
for f in install.sh scripts/update.sh README.md pyproject.toml uv.lock web/package.json; do
  [ -f "$f" ] && ok "$f" || warn "$f MISSING"
done

printf "\n"
bold "Next"
cat <<'TXT'
  git status                 review what this staged
  git commit -m "Prepare for distribution"
  git remote add origin git@github.com:<you>/<repo>.git
  git push -u origin main

  Then a student runs:
    git clone <repo-url> ~/video-studio && cd ~/video-studio && ./install.sh
TXT
