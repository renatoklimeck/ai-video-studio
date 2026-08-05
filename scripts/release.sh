#!/bin/bash
# Publish a new version to the students (REN-168).
#
#   scripts/release.sh "o que mudou"
#
# Versions are CalVer: YYYY.MM.DD.n — 2026.08.04.1, 2026.08.04.2, …
# The n restarts at 1 every day. Chosen over 08_06_26_01 because it sorts
# correctly in every file list and tag list, and because 08_06_26 reads as
# 8 June to a Brazilian and 6 August to an American — with students reading
# it, that ambiguity turns into support messages.
#
# What it does, in order: bump VERSION, prove the interface still builds,
# commit, tag, push. It stops at the first thing that fails, and it will not
# push a version whose build is broken.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

MSG="${1:-}"
[ -n "$MSG" ] || die "diga o que mudou:  scripts/release.sh \"o que mudou\""

# ── checks before touching anything ────────────────────────────────────────
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || die "você está em '$BRANCH'. Publique a partir da main."
git remote get-url origin >/dev/null 2>&1 || die "sem remote 'origin' configurado."

# A release has to be reproducible from the tag, so everything that goes in it
# must be committed. Untracked files are fine — projects/ and .venv/ live here.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no | sed 's/^/     /'
  die "há alterações não commitadas acima. Commite ou descarte antes de publicar."
fi

# ── next version for today ─────────────────────────────────────────────────
TODAY="$(date +%Y.%m.%d)"
CUR="$(cat VERSION 2>/dev/null | tr -d '[:space:]' || true)"
case "$CUR" in
  "$TODAY".*) N=$(( ${CUR##*.} + 1 )) ;;   # já saiu versão hoje
  *)          N=1 ;;
esac
NEXT="$TODAY.$N"

bold "Publicando $NEXT"
[ -n "$CUR" ] && ok "versão atual: $CUR" || ok "primeira versão numerada"

git rev-parse -q --verify "refs/tags/$NEXT" >/dev/null \
  && die "a tag $NEXT já existe. Alguma coisa saiu do lugar — confira 'git tag'."

# ── build antes de commitar ────────────────────────────────────────────────
# Publicar uma versão que não compila deixa todo aluno que atualizar com a
# interface quebrada, e o update.sh deles reverte sozinho — barulho por nada.
bold "Conferindo que a interface compila"
( cd web && npm install --silent && npm run --silent build ) >/dev/null 2>&1 \
  || die "o build falhou. Nada foi publicado."
ok "build ok"

# ── commit + tag + push ────────────────────────────────────────────────────
printf "%s\n" "$NEXT" > VERSION
git add VERSION
git commit --quiet -m "$NEXT — $MSG"
git tag -a "$NEXT" -m "$MSG"
ok "commit e tag criados"

bold "Enviando para o GitHub"
git push --quiet origin main
git push --quiet origin "$NEXT"
ok "no ar — os alunos veem '$NEXT' no botão de atualizar"

# ── reiniciar o SEU servidor ───────────────────────────────────────────────
# O aluno recebe a versão nova pelo update.sh, que termina reiniciando. Aqui na
# sua máquina não existe update: o repositório JÁ é a instalação, então o
# arquivo novo está no disco no instante em que você commita. Só que o processo
# python que está rodando continua com o código VELHO na memória até reiniciar
# — e aí você testa uma coisa que não é a que publicou. Já custou tempo.
bold "Reiniciando o seu servidor"
LABEL=""
for p in "$HOME"/Library/LaunchAgents/*.plist; do
  [ -f "$p" ] || continue
  if grep -q "$PWD/" "$p" 2>/dev/null; then LABEL="$(basename "$p" .plist)"; break; fi
done
if [ -z "$LABEL" ]; then
  warn_msg="sem LaunchAgent para esta pasta — reinicie o servidor você mesmo"
  printf "  \033[33m!\033[0m %s\n" "$warn_msg"
# Um export/transcrição em andamento morre com o restart. Publicar não pode
# custar 20 minutos de render, então nesse caso o restart fica para depois.
elif pgrep -qf "$PWD/render/" 2>/dev/null || pgrep -qx ffmpeg >/dev/null 2>&1; then
  printf "  \033[33m!\033[0m tem render/transcrição rodando — NÃO reiniciei.\n"
  printf "     quando terminar:  launchctl kickstart -k gui/%s/%s\n" "$(id -u)" "$LABEL"
else
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 \
    && ok "$LABEL reiniciado — a sua cópia agora roda a $NEXT de verdade" \
    || printf "  \033[33m!\033[0m não consegui reiniciar %s — faça na mão\n" "$LABEL"
fi

printf "\n"
bold "Publicado: $NEXT"
printf "  %s\n" "$MSG"
printf "  recarregue a aba do app (⌘R) para ver a interface nova\n"
