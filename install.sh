#!/bin/bash
# AI Video Studio — one-command install (macOS).
#
#   ./install.sh
#
# Idempotent: safe to re-run. It only does the steps that are still missing, so
# it doubles as a repair tool when something on the machine breaks.
#
# What it sets up, and why each piece is here:
#   Homebrew + ffmpeg + whisper-cpp   the media and speech engines
#   uv + .venv                        the Python side (analysis, render)
#   whisper models                    small first (the ear, 465MB); large-v3
#                                     (2.9GB) is optional and downloads last
#   mkcert + .certs                   a LOCALLY TRUSTED https cert, so the
#                                     browser does not throw a warning at a
#                                     tool you use every day
#   node + web build                  the editor UI
#   launchd service                   the app runs in the background and comes
#                                     back after a reboot
#
# NOT installed here: the Claude Code CLI. The app runs edits with YOUR OWN
# Claude subscription through it, so it has to be your login, not ours.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS="$HOME/loom-tools/models"
LABEL="com.aivideostudio.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${VSTUDIO_PORT:-3030}"
# 127.0.0.1 keeps the editor on this machine only, which is the right default:
# the chat runs the AI CLI with approvals bypassed, so anything that can reach
# this port can run shell commands as you. Set VSTUDIO_HOST=0.0.0.0 to open it to
# the wifi (to edit from a phone) — step 5b then REFUSES to do that without a PIN.
HOST="${VSTUDIO_HOST:-127.0.0.1}"
PIN_FILE="${VSTUDIO_PIN_FILE:-$HOME/.claude/video-studio-pin}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

step() { printf "\n"; bold "$*"; }

[ "$(uname)" = "Darwin" ] || die "This installer is for macOS. On Linux the same steps work, but the launchd service at the end does not — use systemd."

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
step "1/8  Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "already installed"
else
  warn "not found — installing (this asks for your password)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # a fresh install is not on PATH yet in this shell
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)"
  done
  command -v brew >/dev/null 2>&1 || die "Homebrew installed but is not on PATH. Open a new terminal and run ./install.sh again."
fi

# ── 2. command-line tools ─────────────────────────────────────────────────────
step "2/8  ffmpeg, whisper, node, uv, mkcert"
for pkg in ffmpeg whisper-cpp node uv mkcert git; do
  if command -v "${pkg/whisper-cpp/whisper-cli}" >/dev/null 2>&1; then
    ok "$pkg"
  else
    printf "  installing %s…\n" "$pkg"
    brew install "$pkg"
  fi
done

# ── 3. Python environment ─────────────────────────────────────────────────────
step "3/8  Python environment (.venv)"
cd "$ROOT"
# projects/ is gitignored, so a fresh clone does not have it. The server creates
# it on demand, but the command-line tools do not — and neither does a `cp` of
# somebody else's project into a folder that is not there yet.
mkdir -p "$ROOT/projects"
uv sync --quiet
ok "$(.venv/bin/python --version) with $(grep -c '^ *"' pyproject.toml || echo '') dependencies"

# ── 4. speech models ──────────────────────────────────────────────────────────
step "4/8  speech models"
mkdir -p "$MODELS"
get_model() {  # name url size-note
  local f="$MODELS/$1"
  if [ -f "$f" ]; then ok "$1 already here"; return; fi
  printf "  downloading %s (%s)…\n" "$1" "$3"
  curl -fL --progress-bar -o "$f.part" "$2" && mv "$f.part" "$f"
  ok "$1"
}
BASE="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
# the VAD model lives in a DIFFERENT repo — it is not under whisper.cpp
VAD="https://huggingface.co/ggml-org/whisper-vad/resolve/main"
get_model ggml-small.bin         "$BASE/ggml-small.bin"          "465 MB — the ear"
get_model ggml-silero-v5.1.2.bin "$VAD/ggml-silero-v5.1.2.bin"   "1 MB — voice activity"

if [ -f "$MODELS/ggml-large-v3.bin" ]; then
  ok "ggml-large-v3.bin already here"
elif [ "${VSTUDIO_SKIP_LARGE:-0}" = "1" ]; then
  warn "skipping large-v3 (VSTUDIO_SKIP_LARGE=1) — transcripts will use the small model"
else
  printf "\n  The large model (2.9 GB) gives noticeably better transcripts.\n"
  printf "  You can install it now or later (re-run this script).\n"
  read -r -p "  Download it now? [Y/n] " a </dev/tty || a="n"
  case "${a:-Y}" in
    [Nn]*) warn "skipped — the app falls back to the small model" ;;
    *) get_model ggml-large-v3.bin "$BASE/ggml-large-v3.bin" "2.9 GB" ;;
  esac
fi

# face detector — small, ships with the repo, but heal it if missing
if [ ! -f "$ROOT/render/models/face_detection_yunet_2023mar.onnx" ]; then
  mkdir -p "$ROOT/render/models"
  curl -fL --progress-bar -o "$ROOT/render/models/face_detection_yunet_2023mar.onnx" \
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" || \
    warn "could not fetch the face detector — rehearsal detection by framing will be off"
fi

# face landmarks — the face retouch masks the skin from these 478 points
# (REN-176). Ships with the repo; healed here the same way. Apache 2.0, Google.
# Without it the retouch still works, on the older geometric mask.
if [ ! -f "$ROOT/render/models/face_landmarker.task" ]; then
  mkdir -p "$ROOT/render/models"
  curl -fL --progress-bar -o "$ROOT/render/models/face_landmarker.task" \
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task" || \
    warn "could not fetch the face landmarks — retouch falls back to the rougher mask"
fi

# ── 5. https certificate ──────────────────────────────────────────────────────
step "5/8  local https certificate"
if [ -f "$ROOT/.certs/server.pem" ] && [ -f "$ROOT/.certs/server-key.pem" ]; then
  ok "already generated"
else
  mkdir -p "$ROOT/.certs"
  # installs a local CA into the login keychain, so localhost https is trusted
  # by the browser and there is no warning page every session
  mkcert -install
  ( cd "$ROOT/.certs" && mkcert -cert-file server.pem -key-file server-key.pem \
      localhost 127.0.0.1 ::1 >/dev/null )
  chmod 600 "$ROOT/.certs/server-key.pem"
  ok "generated and trusted"
fi

# ── 5b. a PIN, but only when the app is reachable from outside this machine ───
# The chat runs the AI CLI with approvals bypassed — whatever reaches this port
# can run shell commands as you. On loopback that is you and nobody else. Open it
# to the wifi and it is everyone on the wifi, so this refuses to set that up
# without a PIN. (The server enables its whole auth layer purely by the existence
# of this file; nothing used to create it, so the gate was never armed.)
case "$HOST" in
  127.0.0.1|localhost|::1) : ;;
  *)
    step "5b/8  PIN for network access"
    if [ -s "$PIN_FILE" ]; then
      ok "PIN already set ($PIN_FILE)"
    else
      mkdir -p "$(dirname "$PIN_FILE")"
      GEN="$(od -An -N4 -tu4 /dev/urandom | tr -d ' ' | tail -c 7 | head -c 6)"
      printf "%s" "$GEN" > "$PIN_FILE"
      chmod 600 "$PIN_FILE"
      warn "the app will answer on the whole network ($HOST)"
      printf "     Your PIN is \033[1m%s\033[0m — you will be asked for it once per browser.\n" "$GEN"
      printf "     Change it by editing %s\n" "$PIN_FILE"
    fi
    ;;
esac

# ── 6. the editor UI ──────────────────────────────────────────────────────────
step "6/8  editor interface"
cd "$ROOT/web"
if [ -d node_modules ]; then ok "dependencies present"; else npm install --silent; fi
npm run --silent build
ok "built"
cd "$ROOT"

# ── 7. background service ─────────────────────────────────────────────────────
step "7/8  background service"
mkdir -p "$HOME/Library/LaunchAgents"

# Two services on the same port would fight, and the loser dies in a restart
# loop. If another agent already points at this folder, stand it down first.
for other in "$HOME"/Library/LaunchAgents/*.plist; do
  [ -f "$other" ] || continue
  [ "$other" = "$PLIST" ] && continue
  if grep -q "$ROOT/" "$other" 2>/dev/null; then
    launchctl unload "$other" 2>/dev/null || true
    warn "stopped $(basename "$other" .plist) — it served this same folder"
    printf "     to bring it back instead: launchctl load %s\n" "$other"
  fi
done

# The service must be able to find the AI CLI, node and the brew tools: it does
# not inherit your shell PATH. Collect the real locations, not assumed ones.
SVC_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for c in brew node claude codex ffmpeg whisper-cli uv; do
  p="$(command -v "$c" 2>/dev/null || true)"
  [ -n "$p" ] && SVC_PATH="$(dirname "$p"):$SVC_PATH"
done
[ -d "$HOME/.local/bin" ] && SVC_PATH="$HOME/.local/bin:$SVC_PATH"
# de-duplicate, keep first occurrence
SVC_PATH="$(printf "%s" "$SVC_PATH" | tr ':' '\n' | awk '!seen[$0]++' | paste -sd: -)"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/uvicorn</string>
    <string>server.app:app</string>
    <string>--host</string><string>$HOST</string>
    <string>--port</string><string>$PORT</string>
    <string>--ssl-certfile</string><string>$ROOT/.certs/server.pem</string>
    <string>--ssl-keyfile</string><string>$ROOT/.certs/server-key.pem</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$SVC_PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/server.log</string>
</dict>
</plist>
PLIST_EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
ok "installed and started (restarts itself, survives reboot)"

# ── 8. your AI subscription ───────────────────────────────────────────────────
step "8/8  your AI subscription"
HAVE_AI=0
if command -v claude >/dev/null 2>&1; then ok "Claude Code CLI found"; HAVE_AI=1; fi
if command -v codex  >/dev/null 2>&1; then ok "Codex CLI found";       HAVE_AI=1; fi
if [ "$HAVE_AI" = "0" ]; then
  warn "no AI CLI found — the editor works, but AI edits will not run."
  printf "     Install one and log in with YOUR OWN subscription:\n"
  printf "       Claude:  npm i -g @anthropic-ai/claude-code   then: claude\n"
  printf "       ChatGPT: npm i -g @openai/codex               then: codex\n"
else
  printf "     If AI edits fail with an auth error, run: \033[1mclaude setup-token\033[0m\n"
  printf "     (the normal login expires when the app calls Claude in the background)\n"
fi

# ── ready ─────────────────────────────────────────────────────────────────────
printf "\n"
for _ in $(seq 1 40); do
  curl -sk "https://localhost:$PORT/api/version" >/dev/null 2>&1 && break
  sleep 1
done
if curl -sk "https://localhost:$PORT/api/version" >/dev/null 2>&1; then
  bold "Ready — opening https://localhost:$PORT"
  open "https://localhost:$PORT"
else
  die "the server did not come up. Look at $ROOT/server.log for the reason."
fi
