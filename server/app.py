"""Video Studio server (schema v2): serves the built web app, project CRUD
(create/import/duplicate/delete) with labeled history snapshots, media uploads,
range-aware file streaming for <video>, background jobs (export / proxy /
clip-bg / thumbnails) and the Claude chat bridge (runs `claude -p` headless and
lets it edit project.json directly).

Run: uv run uvicorn server.app:app --host 127.0.0.1 --port 3030
"""
import asyncio
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
import modelscan  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
RENDER = ROOT / "render"
ALLOWED_ROOTS = [PROJECTS, Path.home() / "Movies", Path.home() / "Downloads",
                 Path.home() / "Desktop", Path.home() / "Pictures"]

FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
if not Path(FFMPEG).exists():
    FFMPEG = "ffmpeg"
FFPROBE = str(Path(FFMPEG).with_name(Path(FFMPEG).name.replace("ffmpeg", "ffprobe"))) if "/" in FFMPEG else "ffprobe"

app = FastAPI()
JOBS = {}
THUMB_LAST = {}
HIST_LOCK = threading.Lock()   # history index.json read-modify-write
CHAT_LOCK = threading.Lock()   # chat.json read-modify-write
CHAT_RUNNING = {}              # pid -> job id of an in-flight Claude chat
PENDING_LOCK = threading.Lock()  # pending_edit.json read-modify-write (REN-129)


def _pending_file(d):
    return d / "pending_edit.json"


def park_edit(d, message, model_key, effort, preset_id=None):
    """Persist the parked edit request (survives server restarts — the chat
    promised 'continuo automaticamente'). A second request while one is parked
    is COMBINED, not dropped: both were acknowledged in the chat."""
    with PENDING_LOCK:
        try:
            prev = json.loads(_pending_file(d).read_text())
        except (OSError, ValueError):
            prev = None
        if prev and (prev.get("message") or "").strip() and prev["message"].strip() != message.strip():
            message = prev["message"].rstrip() + "\n\nAlso: " + message
        # The preset id rides along: approving the script resumes this request,
        # and a resume that forgot which preset it came from would quietly run
        # the ordinary AI edit instead of the pass he actually clicked.
        _pending_file(d).write_text(json.dumps(
            {"message": message, "model": model_key, "effort": effort,
             "preset": preset_id or (prev or {}).get("preset")}, ensure_ascii=False))


def pop_pending(d):
    with PENDING_LOCK:
        try:
            data = json.loads(_pending_file(d).read_text())
        except (OSError, ValueError):
            data = None
        try:
            _pending_file(d).unlink()
        except OSError:
            pass
        return data


# ---------- PIN auth (enabled only when the PIN file exists — i.e. on the VPS;
# the local server binds 127.0.0.1 and needs none) ----------
#
# A short PIN is only safe with hard anti-brute-force, so:
#   - per-IP lockout: 5 free attempts, then 30s doubling up to 1h
#   - global circuit breaker: >60 failures/hour locks everyone out for 1h
#   - constant 0.4s delay on every attempt
#   - session = 32-byte random token in an HttpOnly/SameSite=Lax cookie
#     (Secure behind HTTPS), only its sha256 stored server-side, 90-day TTL

PIN_FILE = Path(os.environ.get("VSTUDIO_PIN_FILE",
                               str(Path.home() / ".claude" / "video-studio-pin")))
SESS_FILE = Path(os.environ.get("VSTUDIO_SESSIONS_FILE",
                                str(Path.home() / ".claude" / "video-studio-sessions.json")))
SESSION_TTL = 90 * 24 * 3600
COOKIE_NAME = "vs_session"
AUTH_LOCK = threading.Lock()
AUTH_FAILS = {}                       # ip -> {"count": int, "until": epoch}
AUTH_GLOBAL = {"count": 0, "start": 0.0}
AUTH_PUBLIC_PATHS = {"/", "/index.html", "/favicon.ico", "/favicon.svg",
                     "/favicon-32.png", "/favicon-64.png", "/apple-touch-icon.png",
                     "/api/auth"}
AUTH_PUBLIC_PREFIXES = ("/assets/", "/fonts/")


def auth_required() -> bool:
    return PIN_FILE.exists()


def _load_sessions():
    try:
        d = json.loads(SESS_FILE.read_text())
        return {k: v for k, v in d.items() if v > time.time()}
    except Exception:
        return {}


SESSIONS = _load_sessions()


def _save_sessions():
    SESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    live = {k: v for k, v in SESSIONS.items() if v > time.time()}
    SESS_FILE.write_text(json.dumps(live))
    try:
        SESS_FILE.chmod(0o600)
    except Exception:
        pass


def session_valid(token) -> bool:
    if not token:
        return False
    h = hashlib.sha256(token.encode()).hexdigest()
    exp = SESSIONS.get(h)
    return bool(exp and exp > time.time())


def verify_pin(pin: str) -> bool:
    try:
        data = PIN_FILE.read_text().strip()
    except Exception:
        return False
    if "$" in data:  # salted: "<salt>$sha256(salt+pin)"
        salt, want = data.split("$", 1)
        got = hashlib.sha256((salt + pin).encode()).hexdigest()
        return hmac.compare_digest(got, want)
    return hmac.compare_digest(data, pin)  # plaintext fallback for manual setup


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    if peer in ("127.0.0.1", "::1"):  # trust the reverse proxy's header only
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


def is_local_direct(request: Request) -> bool:
    """Direct loopback connection (the Mac itself, not proxied): no PIN needed.
    A reverse proxy would add X-Forwarded-For, which disables the bypass."""
    peer = request.client.host if request.client else ""
    return peer in ("127.0.0.1", "::1") and "x-forwarded-for" not in request.headers


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if auth_required() and not is_local_direct(request):
        p = request.url.path
        public = p in AUTH_PUBLIC_PATHS or p.startswith(AUTH_PUBLIC_PREFIXES)
        if not public and not session_valid(request.cookies.get(COOKIE_NAME)):
            return JSONResponse({"error": "auth required"}, status_code=401)
    resp = await call_next(request)
    # never cache the app shell — WebKit's heuristic caching kept a days-old UI
    # alive in the standalone app (REN-131); hashed /assets stay cacheable.
    if request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/auth")
def auth_status(request: Request):
    authed = (not auth_required()) or is_local_direct(request) \
        or session_valid(request.cookies.get(COOKIE_NAME))
    return {"required": auth_required(), "authed": authed}


@app.post("/api/auth")
async def auth_login(request: Request):
    if not auth_required():
        return {"ok": True, "required": False}
    body = await request.json()
    pin = str(body.get("pin", ""))[:32]
    ip = client_ip(request)
    now = time.time()

    with AUTH_LOCK:
        if now - AUTH_GLOBAL["start"] > 3600:
            AUTH_GLOBAL["count"], AUTH_GLOBAL["start"] = 0, now
        if AUTH_GLOBAL["count"] > 60:
            retry = int(3600 - (now - AUTH_GLOBAL["start"])) + 1
            return JSONResponse({"ok": False, "retryAfter": retry}, status_code=429)
        rec = AUTH_FAILS.get(ip)
        if rec and rec["until"] > now:
            return JSONResponse({"ok": False, "retryAfter": int(rec["until"] - now) + 1},
                                status_code=429)

    await asyncio.sleep(0.4)  # flat cost per attempt

    if not verify_pin(pin):
        with AUTH_LOCK:
            rec = AUTH_FAILS.setdefault(ip, {"count": 0, "until": 0.0})
            rec["count"] += 1
            AUTH_GLOBAL["count"] += 1
            if rec["count"] >= 5:
                lock = min(3600, 30 * (2 ** (rec["count"] - 5)))
                rec["until"] = now + lock
                return JSONResponse({"ok": False, "retryAfter": int(lock)}, status_code=429)
        return JSONResponse({"ok": False}, status_code=401)

    token = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        AUTH_FAILS.pop(ip, None)
        SESSIONS[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_TTL
        _save_sessions()
    resp = JSONResponse({"ok": True})
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL, httponly=True,
                    samesite="lax", secure=secure, path="/")
    return resp

CLAUDE_BIN = shutil.which("claude") or "/usr/local/bin/claude"
# Long-lived headless auth (`claude setup-token`): paste the token into this
# file so the chat works even when the interactive OAuth session is stale.
CLAUDE_TOKEN_FILE = Path.home() / ".claude" / "video-studio-token"

# Chat model + reasoning effort picker (the UI sends a short key; we map to an
# ENGINE + model id). Allowlisted so the UI can never inject an arbitrary CLI
# arg. "codex" runs the OpenAI Codex CLI with the user's ChatGPT subscription
# (REN-130 — Bootcamp students all have ChatGPT; not all have Claude Max).
CHAT_MODELS = {
    "opus": {"engine": "claude", "id": "claude-opus-4-8"},
    "sonnet": {"engine": "claude", "id": "claude-sonnet-5"},
    "fable": {"engine": "claude", "id": "claude-fable-5"},
    "codex": {"engine": "codex", "id": None},  # the plan's default Codex model
}
CHAT_DEFAULT_MODEL = "claude:claude-opus-5"


def resolve_model(key):
    """UI key → (canonical_key, spec).

    Keys are "<engine>:<model id>" so newly DISCOVERED models work without any
    code change (REN-136). Legacy short keys ('opus', 'sonnet', …) still resolve,
    and anything unrecognised falls back to a model this machine can actually
    run — never to a hardcoded id that may not exist for this user."""
    if isinstance(key, str):
        if key in CHAT_MODELS:                       # legacy short key
            spec = CHAT_MODELS[key]
            return f"{spec['engine']}:{spec['id'] or ''}", spec
        if ":" in key:
            eng, _, mid = key.partition(":")
            if eng in ("claude", "codex") and len(mid) <= 60 and re.fullmatch(r"[A-Za-z0-9._-]*", mid):
                return key, {"engine": eng, "id": mid or None}
    for cand in (CHAT_DEFAULT_MODEL, "claude:claude-opus-4-8", "codex:"):
        eng, _, mid = cand.partition(":")
        if engine_ready(eng):
            return cand, {"engine": eng, "id": mid or None}
    return CHAT_DEFAULT_MODEL, {"engine": "claude", "id": "claude-opus-5"}


def engine_ready(engine):
    if engine == "codex":
        return bool((shutil.which("codex") or Path(CODEX_BIN).exists())
                    and (Path.home() / ".codex" / "auth.json").exists())
    return bool(shutil.which("claude") or Path(CLAUDE_BIN).exists())
# effort → extended-thinking budget. "ultracode" caps the budget like "max" but
# also appends an exhaustive/self-verifying directive to the prompt (below).
CHAT_EFFORT_TOKENS = {"low": 2048, "medium": 10000, "high": 20000,
                      "max": 31999, "ultracode": 31999}
CHAT_DEFAULT_EFFORT = "medium"

# Editor preferences (REN-120): durable, GLOBAL rules for how Renato likes his
# videos edited (take selection, cutting, caption style…). Injected into every
# chat edit, and auto-grown by a distill step after each successful edit.
PREFS_FILE = Path.home() / ".claude" / "video-studio-preferences.md"
PREFS_LOCK = threading.Lock()

# Chat presets (REN-122): one-click prompts the user builds/edits. A preset just
# runs its text through the normal chat. Seeded with the "first edit" take-select.
PRESETS_FILE = Path.home() / ".claude" / "video-studio-presets.json"
# Which built-in presets this machine has already been offered. A default added in
# a later version has to reach installs that already have a presets file, but one
# the user DELETED must stay deleted — the id alone can't tell those apart. Kept
# outside the presets array on purpose: the PUT sanitiser drops any entry without
# a prompt, and an entry with one would show up as a chip.
PRESETS_SEEDED_FILE = Path.home() / ".claude" / "video-studio-presets.seeded.json"
PRESETS_LOCK = threading.Lock()
FIRST_EDIT_PROMPT = """Goal: Turn my raw recording into the cleanest, most finished first cut you can — well-executed and ready to watch, not a rough draft. I'll review and fine-tune whatever I need after you, so make it as done as you can.

Context: I might have made mistakes while recording and sometimes re-recorded the same line several times until I got it right.

Choosing takes
1. Get a word-level transcript of the source, in source time. If one already exists on disk, reuse it — don't re-transcribe.
2. Read the transcript in order and group it into "parts" — each part is one line/sentence I was trying to say. A part can have several back-to-back attempts.
3. For each part, keep exactly ONE take:
   - Only use a take where I said the line correctly and completely, all the way to the end of the thought.
   - If more than one attempt is complete and correct, keep the LAST one — the last good take is the one I settled on before moving on.
   - Drop every other attempt of that part.

Cutting
4. Set each clip's in/out from the transcript word times:
   - Start the clip on the FIRST WORD of the chosen correct version. If that same take has a false start, a stumble, or a filler ("é…", "então…") right before the good version, exclude it — move the in-point to the first good word. A clip must never open on an error I re-said correctly right after.
   - End the clip right after the LAST WORD of the part.
   - Never cut inside a word or inside a sentence.
   - Cut tight: remove ALL the empty space / silence between takes. Never remove pauses inside a sentence.
5. Keep the natural order (source order, minus the discarded takes). Don't reorder or drop real content.

Captions
6. Add word-synced captions that match the FINAL cut exactly — use the fast captions tool the system instructions point you to (instant, generated from the source transcript). Never re-transcribe the cut for captions and never estimate word times by hand.

Mandatory self-check before finishing
- For EVERY clip, re-read the transcript words inside its in/out and confirm the opening is the clean start of the line — not a repeated, stuttered, or error fragment that's re-said right after. If any clip opens on an error, move its in-point forward and fix it.
- Confirm no clip is cut mid-sentence and no kept take is an incomplete attempt.

Reply with 2–4 short sentences: how many parts you kept, roughly how much you cut, confirm you ran the opening self-check, and flag anything ambiguous. If you genuinely can't tell which of two takes is the good one, make the most reasonable choice, keep going, and tell me where — don't stop to ask unless it's truly blocking."""
# The first-pass preset: it CLEANS but never chooses. Deliberately a thin wrapper
# around render/cleanup.py — the keep/drop verdict is deterministic and reviewed,
# and a model improvising here is exactly what loses a good take.
CLEAN_PASS_PROMPT = """Goal: my FIRST PASS on raw footage. Clean it up, but do NOT choose between takes for me.

Run the deterministic cleanup tool. This pass IS that tool, not your own judgement:

  .venv/bin/python render/cleanup.py "<this project's folder — the one holding its project.json>"

What it does, so you know what you are reporting: it reads the approved script, measures every attempt against the line it was delivering, and removes the empty space between takes plus the attempts that were abandoned before finishing their line. Every attempt that DID finish its line stays. If I recorded the same line five times and one of them was wrong, the other four stay in the timeline — that is the whole point. I compare them and choose myself, afterwards.

Rules:
- Do not pick takes, and never drop a take just because another take says the same thing. Repetition is what I asked to keep.
- The tool writes the whole cut. Don't hand-edit clips, captions, texts, overlays or audio before or after it, and don't "improve" its result.
- If it says the source has no transcript yet, run the repo's transcribe_source.py for that source and then run cleanup.py again. That is the ONLY extra step you may take.
- If it refuses or errors for any other reason, tell me exactly what it said and stop. No workaround, no manual cut.

Then reply with 2–4 short sentences built from the numbers the tool printed: takes kept vs dropped, how much dead air went away, the final duration, and anything it flagged for me (lines I recorded several times and now have to choose between, sections it says I never delivered cleanly)."""
DEFAULT_PRESETS = [{"id": "first-edit", "name": "First edit — pick takes",
                    "desc": "Full auto first cut: keeps one take per script line, "
                            "removes the dead air, syncs the captions. Needs an approved script.",
                    "prompt": FIRST_EDIT_PROMPT, "requireScript": True},
                   {"id": "clean-pass", "name": "Clean pass — keep every good take",
                    "desc": "First pass on raw footage: same approved script, then it throws out "
                            "the errors and the dead air and keeps every good take for you to choose.",
                    # Same script step as the full edit: he approves the text
                    # first, and the pass then knows which attempts finish a line
                    # and which were abandoned. It just never picks between the
                    # good ones — that is the only difference between the two.
                    "prompt": CLEAN_PASS_PROMPT, "requireScript": True,
                    # Names the deterministic pass this preset IS. Everything it
                    # decides is measured, so putting the model in the loop only
                    # buys a minute of latency and a chance to improvise. The
                    # prompt above stays as the fallback if the tool can't load.
                    "pipeline": "cleanup"}]
# Defaults that shipped BEFORE the seeded-ids marker existed. They must never be
# appended to a presets file that already exists — the user may have deleted them
# on purpose, and /api/presets runs on every app load.
PRE_MARKER_PRESETS = {"first-edit"}


def read_prefs() -> str:
    """The editing rules injected into every AI edit.

    Seeded from the ones shipped with the app the first time, so a fresh install
    edits the way this app is meant to edit instead of starting with no opinion
    at all — those nine rules are most of the difference between a usable first
    cut and a rough one. After that the file is the user's: it is never
    overwritten, and the app grows it from his own corrections."""
    try:
        return PREFS_FILE.read_text().strip()
    except OSError:
        pass
    try:
        seed = (ROOT / "server" / "default_preferences.md").read_text().strip()
    except OSError:
        return ""
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(PREFS_FILE, seed + "\n")
    except OSError:
        pass          # read-only home: still edit well this session
    return seed


def append_pref(rule: str):
    """Add one durable rule, skipping exact (case-insensitive) duplicates."""
    rule = rule.strip().lstrip("-•* ").strip()
    if not rule:
        return
    with PREFS_LOCK:
        cur = read_prefs()
        existing = {ln.strip().lstrip("-•* ").strip().lower()
                    for ln in cur.splitlines() if ln.strip()}
        if rule.lower() in existing:
            return
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text((cur + "\n" if cur else "") + f"- {rule}\n")


def learn_pref(message: str, reply: str, model_key: str = CHAT_DEFAULT_MODEL):
    """After a successful edit, distill ONE new durable preference (if any) from
    what the creator asked. Cheap, read-only, best-effort/background. Dispatches
    by the edit's engine so codex-only users learn preferences too (REN-130)."""
    try:
        cur = read_prefs() or "(none yet)"
        dp = (
            "You maintain Renato's DURABLE video-editing preferences: GENERAL rules for how "
            "he likes his reels edited (take selection, cutting silence, caption style, pacing) "
            "that apply to FUTURE videos — NOT one-off instructions for a single clip/video.\n\n"
            f"Current preferences:\n{cur}\n\n"
            f"In the in-app editor chat Renato asked: \"{message}\"\n"
            f"The editor applied it and replied: \"{reply}\"\n\n"
            "If this reveals ONE new durable preference not already covered, output it as a single "
            "concise imperative line (no bullet, no quotes, same language Renato used). If it is "
            "video-specific, a one-off, or already covered, output exactly: NONE"
        )
        _, spec = resolve_model(model_key)
        if spec["engine"] == "claude" and (shutil.which("claude") or Path(CLAUDE_BIN).exists()):
            r = subprocess.run(
                [CLAUDE_BIN, "-p", dp, "--model", "claude-haiku-4-5-20251001"],
                cwd=str(ROOT), env=claude_env(), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=120)
        else:
            r = run_agent(dp, model_key, "low", 120)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return
        # take the last non-empty line that isn't a preamble ("Here is the rule:")
        cand = ""
        for ln in reversed([x.strip() for x in out.splitlines() if x.strip()]):
            if not ln.endswith(":"):
                cand = ln
                break
        # reject the nothing-to-learn sentinel even when decorated ("NONE.",
        # "None — already covered", "The answer is NONE") and over-long output
        words = re.sub(r"[^a-z ]", " ", cand.lower()).split()
        if not words or words[0] == "none" or words[-1] == "none" or len(cand) > 240:
            return
        append_pref(cand)
    except Exception:
        pass


def visual_check(d: Path, table, picks, model_key):
    """Render one labelled frame per chosen take into a contact sheet and ask the
    model to LOOK at it. Returns take ids it says are unusable (REN-135)."""
    by_id = {t["id"]: t for t in table["takes"]}
    ids = [tid for p in picks.get("picks", []) for tid in (p.get("takes") or [])]
    ids = [i for i in dict.fromkeys(ids) if i in by_id][:12]
    if len(ids) < 2:
        return []
    proj = read_project(d)
    src = (proj.get("sources") or {}).get(table["src"]) or {}
    media = None
    for cand in (d / "media" / "proxy_main.mp4", d / "media" / "proxy_source.mp4"):
        if cand.exists():
            media = cand
            break
    media = media or resolve_media(d, src.get("path"))
    if not media:
        return []
    tmpdir = Path(tempfile.mkdtemp(prefix="vsheet_"))
    try:
        shots = []
        for tid in ids:
            t = by_id[tid]
            at = t["in"] + (t["out"] - t["in"]) * 0.45
            out = tmpdir / f"t{tid}.jpg"
            subprocess.run(
                [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(media),
                 "-frames:v", "1", "-vf",
                 f"scale=260:-2,drawtext=text='TAKE {tid}':fontsize=26:fontcolor=yellow:"
                 "box=1:boxcolor=black@0.75:x=6:y=6", str(out)],
                capture_output=True, timeout=60)
            if out.exists():
                shots.append(out)
        if len(shots) < 2:
            return []
        sheet = tmpdir / "sheet.jpg"
        cmd = [FFMPEG, "-y", "-loglevel", "error"]
        for s in shots:
            cmd += ["-i", str(s)]
        cmd += ["-filter_complex", f"{''.join(f'[{i}]' for i in range(len(shots)))}"
                f"hstack=inputs={len(shots)}", str(sheet)]
        subprocess.run(cmd, capture_output=True, timeout=120)
        if not sheet.exists():
            return []
        q = (f"Look at the image at {sheet}. It shows one frame from each take chosen for a "
             "talking-head video, labelled TAKE <n>. Report any take where the speaker is NOT "
             "properly presenting to camera — out of frame, looking away/down, face cut off, "
             "clearly a rehearsal rather than a real delivery. Judge only what you can see. "
             'Reply ONLY as JSON: {"reject":[take numbers]} — empty list if all are fine.')
        r = run_agent(q, model_key, "low", 240, cwd=str(tmpdir))
        m = re.search(r"\{.*\}", r.stdout or "", re.S)
        if not m:
            return []
        return [int(x) for x in (json.loads(m.group(0)).get("reject") or []) if int(x) in by_id]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


CUT_WORDS = ("edit", "cut", "take", "corte", "corta", "edite", "edição", "edicao",
             "editar", "monte", "montar", "primeira edi", "first edit", "assemble",
             "escolh", "choose", "pick", "trim", "silêncio", "silencio", "silence")
TWEAK_WORDS = ("legenda", "caption", "subtitle", "fonte", "font", "cor", "color",
               "tamanho", "size", "posi", "music", "música", "musica", "volume",
               "zoom", "headline", "título", "titulo", "overlay", "retouch")


def wants_first_cut(message: str) -> bool:
    """True when the message is asking for the CUT itself, not a tweak."""
    m = (message or "").lower()
    if any(w in m for w in CUT_WORDS):
        # a message that is only about captions/style is a tweak even if it says
        # "corta" somewhere; require a cut word that isn't inside a tweak ask
        if any(w in m for w in TWEAK_WORDS) and not any(
                w in m for w in ("primeira edi", "first edit", "escolh", "take")):
            return False
        return True
    return False


def _is_unedited(proj):
    """A project still holding one clip that spans (almost) the whole source —
    i.e. nothing has been cut yet, so a request means 'make the first cut'."""
    clips = proj.get("clips") or []
    if len(clips) != 1:
        return False
    src = (proj.get("sources") or {}).get(clips[0].get("src", "main")) or {}
    dur = float(src.get("duration") or 0)
    return dur > 0 and (clips[0]["out"] - clips[0]["in"]) >= dur * 0.9


def run_cleanup_pipeline(d: Path, force=False):
    """First pass on raw footage — NO model in the loop at all.

    Nothing here is a judgement call the AI is better at: the takes, their
    boundaries and the junk signals are all measured. Handing this to the model
    only adds a minute of latency and a chance to improvise. Click → cut.

    Returns a chat reply string, or None to fall back to the free-form edit."""
    sys.path.insert(0, str(RENDER))
    try:
        from cleanup import cleanup as run_cleanup  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    proj = read_project(d)
    src_key = (proj.get("clips") or [{}])[0].get("src", "main")
    if not any(k == src_key for k, _ in source_transcripts(d)):
        return ("⚠ This source has no usable transcript (missing, or it belongs to a "
                "different video). Transcribe it again in the Cut tab, then try again.")
    try:
        res = run_cleanup(d, src_key, force=force)
    except SystemExit as e:
        return f"⚠ {e}"
    except Exception as e:  # noqa: BLE001
        return f"⚠ the cleanup pass failed: {e}"
    if not res.get("ok"):
        why = "; ".join(res.get("problems") or ["unknown reason"])
        # The hand-edit refusal is not a failure, it is a question: he may well
        # want his own cut replaced, but never without being asked.
        return f"⚠ Nothing was changed — {why}"
    lines = list(res.get("report") or [])
    for w in res.get("warnings") or []:
        lines.append(f"⚠ {w}")
    return "\n".join(lines) if lines else "Done."


def run_take_pipeline(pid, d: Path, model_key, effort):
    """Deterministic first cut (REN-134). The model never writes a timestamp:
    code segments the source into takes with audio-accurate boundaries and
    delivery metrics, the model only answers "script line N → take K", then code
    assembles the clips and regenerates captions from the transcript LAST.

    Returns a chat reply string, or None to fall back to the free-form edit."""
    sys.path.insert(0, str(RENDER))
    try:
        from takes import build_takes, as_table  # noqa: PLC0415
        from assemble import assemble           # noqa: PLC0415
    except Exception:
        return None
    proj = read_project(d)
    script_lines = [l for l in (proj.get("script") or "").splitlines() if l.strip()]
    if not script_lines:
        return None
    src_key = (proj.get("clips") or [{}])[0].get("src", "main")
    # the cut is timed from the transcript — refuse a stale one (replaced source)
    if not any(k == src_key for k, _ in source_transcripts(d)):
        return ("⚠ This source has no usable transcript (missing, or it belongs to a "
                "different video). Transcribe it again in the Cut tab, then ask for the edit.")
    try:
        table = build_takes(d, src_key)
    except SystemExit as e:
        return f"⚠ {e}"
    except Exception:
        return None
    if table.get("issues"):
        return "⚠ I could not segment the takes safely: " + "; ".join(table["issues"][:2])

    # MEASURE FIRST, ASK SECOND.
    #
    # Choosing a take is not a judgement call once the script is approved: it is
    # "which attempt says this line, all the way to the end, without a flag on
    # it". Code answers that in a second and gets it right; the model takes a
    # minute of thinking and got it wrong on the last two videos — once badly
    # enough to dead-end the whole edit. So try the arithmetic first and only
    # spend the round trip when it cannot cover every line.
    try:
        from cleanup import picks_from_script  # noqa: PLC0415
        auto, trusted, alts = picks_from_script(d, src_key, table["takes"], script_lines)
        if len(auto) == len(script_lines):
            # THE EYES. Numbers catch the obvious rehearsal — out of position,
            # mumbled, abandoned. They cannot see eyes closed, a hand in shot or
            # the wrong expression; only looking at the frame does. Worth one
            # image round trip, and ONLY where it can change the answer: on a
            # line with a single usable attempt, rejecting it buys nothing.
            swapped = 0
            if alts:
                rejected = set(visual_check(d, table, {"picks": auto}, model_key) or [])
                for p in auto:
                    if not (rejected & set(p["takes"])):
                        continue
                    for other in alts.get(p["line"]) or []:
                        if not (rejected & set(other)):
                            p["takes"] = other
                            swapped += 1
                            break
            res = assemble(d, {"src": src_key, "picks": auto}, trusted=trusted)
            if res.get("ok"):
                try:
                    snapshot(d, read_project(d), "First cut", "claude")
                except Exception:  # noqa: BLE001
                    pass
                return (f"Primeira edição pronta: {res['clips']} cortes "
                        f"({res['duration']:.0f}s) cobrindo as {len(script_lines)} linhas do "
                        f"roteiro, e {res['captions']} grupos de legenda gerados do corte final."
                        + (f" Ouvi {len(trusted)} take(s) isolados para conferir uma flag que "
                           "estava errada." if trusted else "")
                        + (f" Olhei os frames e troquei {swapped} take(s) por uma versão melhor "
                           "enquadrada." if swapped else ""))
    except Exception:  # noqa: BLE001 — never let the shortcut break the real path
        pass

    numbered = "\n".join(f"{i}. {l}" for i, l in enumerate(script_lines, 1))
    ask = f"""You are assembling a video from pre-cut TAKES. You do NOT write timestamps — you only choose take numbers.

APPROVED SCRIPT (every line must appear exactly once, in this order):
{numbered}

{as_table(table)}

Rules:
- For each script line, choose the take(s) whose words say that line. Prefer ONE take.
- NEVER choose a take flagged FALSE START (an abandoned attempt — the good one is
  a later take), QUIET (a rehearsal mumble) or "fragment". These are rejected.
- Prefer takes marked ok; among equally good ones prefer the LAST recorded.
- If a line is split across consecutive takes, list them in order.
- A take marked "continues" runs straight on from the one before it. Picking both
  is GOOD: they are joined with no cut and the pause between them is kept, because
  that pause is deliberate delivery. Do not avoid a take just because it continues.
Output ONLY compact JSON, nothing else: {{"picks":[{{"line":1,"takes":[3]}}]}}"""

    picks = None
    for attempt in range(2):
        r = run_agent(ask if attempt == 0 else ask + "\n\nOutput ONLY the JSON object.",
                      model_key, effort, 600)
        m = re.search(r"\{.*\}", r.stdout or "", re.S)
        if m:
            try:
                picks = json.loads(m.group(0))
                break
            except ValueError:
                picks = None
    if not picks or not picks.get("picks"):
        return None  # fall back to the old free-form path

    # EYES: the model now LOOKS at a frame from each chosen take. Numbers catch
    # the obvious rehearsal (out of frame, quiet); only actually seeing the
    # frames catches eyes-closed, hand in shot, wrong expression (REN-135).
    try:
        rejected = visual_check(d, table, picks, model_key)
        if rejected:
            bad = ", ".join(str(x) for x in rejected)
            ask2 = (ask + f"\n\nA visual check REJECTED takes {bad} (badly framed / not "
                    "addressing the camera). Choose different takes for those lines.")
            r2 = run_agent(ask2, model_key, effort, 600)
            m2 = re.search(r"\{.*\}", r2.stdout or "", re.S)
            if m2:
                try:
                    again = json.loads(m2.group(0))
                    if again.get("picks"):
                        picks = again
                except ValueError:
                    pass
    except Exception:
        pass

    picks["src"] = src_key
    try:
        res = assemble(d, picks)
    except SystemExit as e:
        return f"⚠ I could not assemble the cut: {e}"
    except Exception as e:  # noqa: BLE001
        return f"⚠ I could not assemble the cut: {e}"
    if not res.get("ok"):
        # show the model its OWN errors and let it correct — a rejection used to
        # be terminal, throwing away the whole (expensive) pipeline run
        probs = "; ".join(res.get("problems", [])[:6])
        fix = run_agent(ask + f"\n\nYour previous answer was REJECTED: {probs}\n"
                              "Fix those specific problems and output the corrected JSON only.",
                        model_key, effort, 600)
        m = re.search(r"\{.*\}", fix.stdout or "", re.S)
        if m:
            try:
                retry = json.loads(m.group(0))
                retry["src"] = src_key
                res = assemble(d, retry)
            except Exception:  # noqa: BLE001
                pass
    if not res.get("ok"):
        # LAST RESORT, and the one that matters: stop asking the model and work
        # the answer out from the audio. "Nothing was changed" is the worst
        # possible outcome — he cannot fix it from the app, and it happens for a
        # reason he cannot see (one take flagged FALSE START by mistake was
        # enough to dead-end a whole edit whose correct cut was sitting there).
        try:
            from cleanup import picks_from_script  # noqa: PLC0415
            auto, trusted, _alts = picks_from_script(d, src_key, table["takes"], script_lines)
            if auto:
                res2 = assemble(d, {"src": src_key, "picks": auto}, trusted=trusted)
                if res2.get("ok"):
                    res = res2
                    covered = {p["line"] for p in auto}
                    missing = [i for i in range(1, len(script_lines) + 1) if i not in covered]
                    note = ("\n⚠ Linha(s) " + ", ".join(map(str, missing)) +
                            " não têm nenhuma tentativa completa na gravação — ficaram de fora."
                            ) if missing else ""
                    return (f"Primeira edição pronta: {res['clips']} cortes ({res['duration']:.0f}s), "
                            f"{res['captions']} grupos de legenda. O plano do modelo foi recusado, "
                            f"então escolhi os takes medindo cada tentativa contra o roteiro"
                            + (f" (e ouvindo {len(trusted)} take(s) isolados para conferir uma flag "
                               "que estava errada)" if trusted else "") + f".{note}")
        except Exception:  # noqa: BLE001 — the fallback must never mask the real error
            pass
        return ("⚠ Nothing was changed — the plan was rejected: " + "; ".join(res.get("problems", [])[:3]))
    lines = len({p.get("line") for p in picks["picks"]})
    return (f"Primeira edição pronta: {res['clips']} cortes ({res['duration']:.0f}s) cobrindo as "
            f"{lines} linhas do roteiro, e {res['captions']} grupos de legenda gerados do corte "
            f"final. Nenhum take marcado como ensaio foi usado, e todo corte cai no silêncio.")


def source_transcripts(d: Path):
    """[(src_key, abs transcript path)] for sources with a FRESH transcript on
    disk. A transcript carrying a src_size that no longer matches the media file
    belongs to an older/replaced source — excluded so the AI never edits from
    the wrong video's words (REN-127). Legacy transcripts without the field pass."""
    out = []
    try:
        proj = read_project(d)
        healed = False
        for k, s in (proj.get("sources") or {}).items():
            tp = s.get("transcript")
            if not (tp and (d / tp).exists()):
                # The file can be on disk with nothing pointing at it — a client
                # autosave carrying a stale `sources` overwrites the pointer the
                # transcription just wrote. Trust the conventional path and put
                # the pointer back, instead of telling him to transcribe a video
                # that is already transcribed.
                conv = f"media/transcript_{k}.json"
                if not (d / conv).exists():
                    continue
                tp = conv
                s["transcript"] = conv
                healed = True
            try:
                sig = json.loads((d / tp).read_text()).get("src_size")
                media = d / s.get("path", "") if not str(s.get("path", "")).startswith("/") else Path(s["path"])
                if sig is not None and media.is_file() and media.stat().st_size != sig:
                    continue  # stale — from a different source file
            except (OSError, ValueError):
                pass
            out.append((k, str((d / tp).resolve())))
        if healed:
            try:
                write_project(d, proj)
            except OSError:
                pass
    except OSError:
        pass
    return out


def audit_openings(d: Path, model_key: str):
    """Third layer (REN-119): a focused single-purpose pass that trims any clip
    OPENING on a false start / error re-said right after. Returns a one-line
    status, or None. Separate from the main edit → more reliable than the inline
    self-check. Read-only w.r.t. take choices; only moves clip.in of bad openings."""
    tx = source_transcripts(d)
    if not tx:
        return None
    pj = d / "project.json"
    tx_ref = "\n".join(f'  - source "{k}": {p}' for k, p in tx)
    ap = f"""Audit ONE defect only in this take-selection edit: a clip that OPENS on a false start / stumble / error that is then re-said correctly right after, inside the same clip.

Project file: {pj}
Source word-level transcripts (t0/t1 in SOURCE seconds):
{tx_ref}

For EACH clip in the project:
- Read the transcript words of that clip's source between clip.in and clip.out.
- If the FIRST words are a botched / partial / repeated attempt of a phrase that is re-said cleanly a moment later within the same clip, move clip.in FORWARD to the transcript t0 of the first word of the CORRECT version, and drop any now-orphaned source-anchored caption words that fall before the new in.
- If the opening is already the clean start of the phrase, leave that clip unchanged.

Change ONLY clip.in of clips that open on an error (and their captions). Do NOT change take choices, order, out points, or anything else. Keep {pj} valid schema-v2 JSON with "by":"claude". Re-read it to confirm it parses.
Reply with ONE short line: how many clip openings you trimmed, e.g. "trimmed 2 clip openings" or "openings already clean"."""
    try:
        r = run_agent(ap, model_key, "high", 600)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out.splitlines()[-1].strip()[:200]
    except Exception:
        pass
    return None


def claude_env(effort=None):
    """Child env for `claude -p`: strip this process's Claude/Anthropic vars
    (they may point at another session) and inject the setup-token if present.
    Also widen PATH — under launchd/PM2 the parent PATH can be minimal and the
    claude CLI needs `node` on it. `effort` sets the extended-thinking budget."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k.startswith("ANTHROPIC") or k == "CLAUDECODE")}
    path = env.get("PATH", "/usr/bin:/bin")
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    env["PATH"] = path
    if effort in CHAT_EFFORT_TOKENS:
        env["MAX_THINKING_TOKENS"] = str(CHAT_EFFORT_TOKENS[effort])
    if CLAUDE_TOKEN_FILE.exists():
        # join all whitespace: pasted tokens often carry wrapped newlines
        tok = "".join(CLAUDE_TOKEN_FILE.read_text().split())
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def redact_secrets(text: str) -> str:
    """Never let token-looking strings reach chat bubbles or logs."""
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "sk-ant-***", text or "")


CODEX_BIN = shutil.which("codex") or "/opt/homebrew/bin/codex"
CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high",
                "max": "high", "ultracode": "high"}


def codex_env():
    """Child env for `codex exec`: keep the user's own Codex auth (~/.codex),
    widen PATH like claude_env. Codex signs in with the ChatGPT subscription."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k.startswith("ANTHROPIC") or k == "CLAUDECODE")}
    path = env.get("PATH", "/usr/bin:/bin")
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    env["PATH"] = path
    return env


def run_agent(prompt, model_key, effort, timeout, cwd=None):
    """Run the chosen AI engine headless with full tool access. Returns a
    CompletedProcess whose .stdout is the agent's FINAL reply text.
    claude → claude -p (subscription via setup-token); codex → codex exec
    (ChatGPT subscription via `codex login`). Same trust model either way:
    the local single-user app runs with approvals bypassed (user opted in)."""
    _, spec = resolve_model(model_key)
    cwd = cwd or str(ROOT)
    if spec["engine"] == "codex":
        if not (CODEX_BIN and Path(CODEX_BIN).exists()):
            return subprocess.CompletedProcess(
                ["codex"], 127, "",
                "Codex CLI not installed — `brew install codex` and run `codex login`.")
        fd, outp = tempfile.mkstemp(prefix="codex_reply_", suffix=".txt")
        os.close(fd)
        try:
            # --ignore-user-config: a student's ~/.codex/config.toml (custom model/
            # provider/hooks) must not hijack headless runs; auth still applies.
            cmd = [CODEX_BIN, "exec", "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", "--ignore-user-config", "-o", outp,
                   "-c", f"model_reasoning_effort={CODEX_EFFORT.get(effort, 'medium')}"]
            if spec.get("id"):
                cmd += ["-m", spec["id"]]
            cmd.append(prompt)
            r = subprocess.run(cmd, cwd=cwd, env=codex_env(), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=timeout)
            try:
                reply = Path(outp).read_text().strip()
            except OSError:
                reply = ""
            if not reply:  # -o file empty on failure → surface the stream tail
                reply = (r.stdout or "").strip()[-2000:]
            return subprocess.CompletedProcess(cmd, r.returncode, reply, r.stderr)
        finally:
            try:
                os.unlink(outp)
            except OSError:
                pass
    if not (shutil.which("claude") or Path(CLAUDE_BIN).exists()):
        return subprocess.CompletedProcess(
            ["claude"], 127, "",
            "Claude CLI not installed — install Claude Code and run `claude setup-token`.")
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", spec["id"],
           "--permission-mode", "bypassPermissions"]
    return subprocess.run(cmd, cwd=cwd, env=claude_env(effort), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def engine_auth_error(model_key, err: str):
    """Map raw CLI failures to a friendly, actionable message (or None)."""
    _, spec = resolve_model(model_key)
    low = (err or "").lower()
    if "not installed" in low:
        return None  # the not-installed sentinel is already the right message
    if spec["engine"] == "codex":
        # anchored phrases only — the install sentinel itself contains "login"
        if "not logged in" in low or "unauthorized" in low or "401" in (err or ""):
            return ("Codex não está logado — abra o Terminal e rode `codex login` "
                    "(usa sua assinatura do ChatGPT).")
        return None
    if ("401" in (err or "") or "authentication" in low
            or "not logged in" in low or "credentials" in low):
        return ("Claude não está logado — rode `claude setup-token` no Terminal e salve "
                "o token em ~/.claude/video-studio-token (usa sua assinatura do Claude).")
    return None


# ---------- helpers ----------

PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def pdir(pid: str) -> Path:
    # strict id check: rejects '.', '..', separators — a bad pid must never
    # resolve to the projects root itself or anything outside it
    if not PID_RE.fullmatch(pid) or pid in (".", ".."):
        raise HTTPException(404, "project not found")
    d = (PROJECTS / pid).resolve()
    if d.parent != PROJECTS.resolve() or not d.is_dir():
        raise HTTPException(404, "project not found")
    return d


def project_media(d: Path, p: str) -> Path:
    """Resolve a client-supplied media path: absolute goes through safe_path,
    relative must stay inside the project dir."""
    if Path(p).is_absolute():
        return safe_path(p)
    q = (d / p).resolve()
    if not str(q).startswith(str(d.resolve()) + "/") or not q.is_file():
        raise HTTPException(404, f"file not found: {p}")
    return q


def safe_path(p: str) -> Path:
    q = Path(p).expanduser().resolve()
    if not any(str(q).startswith(str(r.resolve())) for r in ALLOWED_ROOTS):
        raise HTTPException(403, f"path outside allowed roots: {q}")
    if not q.is_file():
        raise HTTPException(404, f"file not found: {q}")
    return q


def slug(s: str) -> str:
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", s.lower())) or "project"


def has_audio(path) -> bool:
    """True if the media has at least one audio stream."""
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-select_streams", "a",
                            "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=15)
        return bool(r.stdout.strip())
    except Exception:
        return False


def probe(path):
    """Return (w, h, duration) of a media file via ffprobe."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams",
             "-show_format", str(path)], capture_output=True, text=True, timeout=30)
        info = json.loads(r.stdout)
        vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        w, h = int(vs.get("width", 0)), int(vs.get("height", 0))
        rot = 0
        for sd in vs.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(sd["rotation"]))
        if rot in (90, 270):
            w, h = h, w
        dur = float(info.get("format", {}).get("duration") or vs.get("duration") or 0)
        return w, h, dur
    except Exception:
        return 0, 0, 0


def run_job(cmd, cwd=None, total_hint=100, on_done=None):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "progress": 0, "total": total_hint,
                 "log": [], "result": None, "started": time.time()}

    def work():
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        a, b = line.split()[1].split("/")
                        JOBS[jid]["progress"], JOBS[jid]["total"] = int(a), int(b)
                    except Exception:
                        pass
                elif line.startswith("done: "):
                    JOBS[jid]["result"] = line[6:]
                else:
                    JOBS[jid]["log"] = (JOBS[jid]["log"] + [line])[-40:]
            rc = proc.wait()
            if rc == 0 and on_done:
                try:
                    on_done(JOBS[jid])
                except Exception as e:  # noqa: BLE001
                    JOBS[jid]["log"].append(f"on_done: {e}")
            JOBS[jid]["status"] = "done" if rc == 0 else "error"
        except Exception as e:  # noqa: BLE001
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"].append(str(e))

    threading.Thread(target=work, daemon=True).start()
    return jid


# Derived-media jobs (proxy / filmstrip / waveform): several clients healing
# the same project must share one ffmpeg run, and readers must never see a
# half-written file — render to .tmp and os.replace() onto the final path.
MEDIA_JOBS: dict = {}
MEDIA_LOCK = threading.Lock()


def media_job(key: str, out: Path, make_cmd, meta: dict):
    """Start (or join) the job producing `out`. Returns meta + {"job": id},
    or meta + {"done": True} when the file already exists (no job to poll)."""
    with MEDIA_LOCK:
        running = MEDIA_JOBS.get(key)
        if running and JOBS.get(running["job"], {}).get("status") == "running":
            return {**running["meta"], "job": running["job"]}
        if out.exists():
            return {**meta, "done": True}
        tmp = out.with_name(f"{out.stem}.tmp{out.suffix}")
        jid = run_job(make_cmd(tmp), on_done=lambda job: os.replace(tmp, out))
        MEDIA_JOBS[key] = {"job": jid, "meta": meta}
        return {**meta, "job": jid}


def read_project(d: Path):
    return json.loads((d / "project.json").read_text())


def write_project(d: Path, data: dict):
    (d / "project.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))


def proxy_vf(w, h):
    """720p proxy = 720 on the SHORT side."""
    return "scale=720:-2" if w <= h else "scale=-2:720"


def proxy_cmd(src, out, w, h):
    """Preview proxy tuned for editing: 720p short side, keyframe every 15
    frames (instant seeks at cuts/scrubbing — the CapCut trick), 8-bit yuv420p
    (iPhone HEVC is often 10-bit), faststart for range streaming."""
    return [FFMPEG, "-y", "-i", str(src), "-vf", proxy_vf(w, h),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-g", "15", "-x264-params", "scenecut=0",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "160k", str(out)]


def resolve_media(d: Path, p):
    if not p:
        return None
    q = Path(p)
    return q if q.is_absolute() else d / q


def gen_thumb(d: Path, force=False):
    """Extract the first clip's first frame as thumb.jpg (background thread)."""
    now = time.time()
    if not force and now - THUMB_LAST.get(d.name, 0) < 8:
        return
    THUMB_LAST[d.name] = now

    def work():
        try:
            p = read_project(d)
            clips = p.get("clips") or []
            if not clips:
                (d / "thumb.jpg").unlink(missing_ok=True)
                return
            src = (p.get("sources") or {}).get(clips[0].get("src", "main")) or {}
            media = resolve_media(d, src.get("proxy") or src.get("path"))
            if not media or not media.is_file():
                return
            subprocess.run([FFMPEG, "-y", "-ss", f"{clips[0]['in']:.3f}", "-i", str(media),
                            "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "4",
                            str(d / "thumb.jpg")], capture_output=True, timeout=60)
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


# ---------- history snapshots ({label, author, ts} metadata) ----------

def hist_dir(d: Path) -> Path:
    h = d / "history"
    h.mkdir(exist_ok=True)
    return h


def hist_index(d: Path):
    f = hist_dir(d) / "index.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return []
    return []


def hist_write_index(d: Path, idx):
    (hist_dir(d) / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1))


def snapshot(d: Path, data: dict, label: str, author: str, dedupe_autosave=False):
    """Append a snapshot (newest first). With dedupe_autosave, a leading
    'Auto-save' entry is updated in place instead of stacking."""
    with HIST_LOCK:
        idx = hist_index(d)
        if dedupe_autosave and idx and idx[0]["label"] == "Auto-save":
            entry = idx[0]
            entry["ts"] = time.time()
            entry["author"] = author
            (hist_dir(d) / entry["file"]).write_text(json.dumps(data, ensure_ascii=False, indent=1))
            hist_write_index(d, idx)
            return
        fname = f"snap-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
        (hist_dir(d) / fname).write_text(json.dumps(data, ensure_ascii=False, indent=1))
        idx.insert(0, {"file": fname, "label": label, "author": author, "ts": time.time()})
        for old in idx[100:]:
            (hist_dir(d) / old["file"]).unlink(missing_ok=True)
        hist_write_index(d, idx[:100])


# ---------- projects ----------

def project_card(d: Path):
    try:
        p = read_project(d)
    except Exception:
        return None
    if p.get("version") != 2:
        return None
    dur = sum(c["out"] - c["in"] for c in p.get("clips", []))
    return {
        "id": d.name, "name": p.get("name", d.name),
        "w": p.get("w"), "h": p.get("h"), "aspect": p.get("aspect", "9:16"),
        "dur": round(dur, 3), "clips": len(p.get("clips", [])),
        "by": p.get("by", "you"),
        "editedAt": int((d / "project.json").stat().st_mtime * 1000),
        "thumb": f"/file?path={d / 'thumb.jpg'}" if (d / "thumb.jpg").exists() else None,
    }


@app.get("/api/projects")
def list_projects():
    out = []
    for d in PROJECTS.iterdir() if PROJECTS.exists() else []:
        if (d / "project.json").exists():
            card = project_card(d)
            if card:
                out.append(card)
                if not (d / "thumb.jpg").exists():
                    gen_thumb(d)
    out.sort(key=lambda c: -c["editedAt"])
    return out


def empty_project(name, w=1080, h=1920):
    return {"version": 2, "name": name, "w": w, "h": h,
            "aspect": "16:9" if w >= h else "9:16", "fps": 30, "by": "you",
            "sources": {}, "clips": [], "captions": [], "texts": [],
            "overlays": [], "audios": []}


def new_pid(name):
    base = slug(name)
    pid = base
    i = 2
    while (PROJECTS / pid).exists():
        pid = f"{base}-{i}"
        i += 1
    return pid


@app.post("/api/projects")
async def create_project(request: Request):
    """Start-empty project. Import-with-video goes through /api/projects/import."""
    body = await request.json()
    name = body.get("name") or "Untitled project"
    pid = new_pid(name)
    d = PROJECTS / pid
    d.mkdir(parents=True)
    write_project(d, empty_project(name))
    return {"id": pid, "project": get_project(pid)}


@app.post("/api/projects/import")
async def import_project(file: UploadFile):
    """Create a project from a source video: probe metadata, 1 clip [0, dur],
    kick a 540p proxy job + thumbnail."""
    name = re.sub(r"\.[^.]+$", "", Path(file.filename or "Imported video").name)
    pid = new_pid(name)
    d = PROJECTS / pid
    (d / "media").mkdir(parents=True)
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    dest = d / "media" / f"source{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    w, h, dur = probe(dest)
    if not w or not h:
        w, h, dur = 1080, 1920, 12.0
    dur = dur or 12.0
    clip_out = max(dur, 2.0)  # the whole source lands on the timeline (min 2s floor)
    p = empty_project(name, w, h)
    p["sources"] = {"main": {"path": f"media/{dest.name}", "proxy": None, "duration": round(dur, 3)}}
    p["clips"] = [{"id": "c" + uuid.uuid4().hex[:6], "src": "main", "in": 0.0,
                   "out": round(clip_out, 3), "fadeIn": 0, "fadeOut": 0, "kfs": [],
                   "bg": None, "rt": None}]
    write_project(d, p)
    proxy_out = d / "media" / "proxy_main.mp4"
    r = media_job(f"proxy:{dest}", proxy_out,
                  lambda tmp: proxy_cmd(dest, tmp, w, h),
                  {"path": "media/proxy_main.mp4"})
    jid = r.get("job")

    def attach_proxy():
        if jid:
            while JOBS[jid]["status"] == "running":
                time.sleep(0.5)
            if JOBS[jid]["status"] != "done":
                return
        try:
            cur = read_project(d)
            cur["sources"]["main"]["proxy"] = "media/proxy_main.mp4"
            write_project(d, cur)
            gen_thumb(d, force=True)
        except Exception:
            pass
        # …then get the words, right now, while he is still looking at the video.
        #
        # Transcribing eight minutes takes two minutes, and it used to happen
        # INSIDE the first edit: he clicked the preset and watched a spinner for
        # the whole of it, on the AI's clock, which is also what made that step
        # time out. Nothing about it depends on what he asks for, so it has no
        # business being on the critical path. Started here it is almost always
        # finished before he clicks anything.
        try:
            prewarm_transcript(d, "main")
        except Exception:  # noqa: BLE001 — an import must never fail over this
            pass

    threading.Thread(target=attach_proxy, daemon=True).start()
    gen_thumb(d, force=True)
    return {"id": pid, "proxyJob": jid, "project": get_project(pid)}


@app.post("/api/project/{pid}/take")
async def import_take(pid: str, file: UploadFile):
    """Import a source video as a NEW take: add to sources{}, kick proxy job.
    The client appends the clip (detecting aspect if the project was empty)."""
    d = pdir(pid)
    (d / "media").mkdir(exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(file.filename or "take").stem)[:40]
    # unique key — wall-clock seconds collide when several takes are imported in
    # the same second (e.g. a multi-file drag&drop), which would overwrite one
    # source and make two ffmpeg jobs fight over the same proxy path
    key = "s" + uuid.uuid4().hex[:10]
    dest = d / "media" / f"{key}_{stem}{Path(file.filename or 'v.mp4').suffix or '.mp4'}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    w, h, dur = probe(dest)
    proxy_out = d / "media" / f"proxy_{key}.mp4"
    r = media_job(f"proxy:{dest}", proxy_out,
                  lambda tmp: proxy_cmd(dest, tmp, w or 1080, h or 1920),
                  {"path": f"media/{proxy_out.name}"})
    return {"key": key, "path": f"media/{dest.name}", "proxy": r["path"],
            "proxyJob": r.get("job"), "w": w, "h": h, "duration": round(dur, 3)}


@app.post("/api/project/{pid}/duplicate")
def duplicate_project(pid: str):
    d = pdir(pid)
    p = read_project(d)
    name = p.get("name", pid) + " copy"
    nid = new_pid(name)
    nd = PROJECTS / nid
    shutil.copytree(d, nd, ignore=shutil.ignore_patterns("exports", "history"))
    np = read_project(nd)
    np["name"] = name
    np["by"] = "you"
    write_project(nd, np)
    return {"id": nid}


@app.delete("/api/project/{pid}")
def delete_project(pid: str):
    d = pdir(pid)
    shutil.rmtree(d)
    return {"ok": True}


@app.get("/api/project/{pid}")
def get_project(pid: str):
    d = pdir(pid)
    data = read_project(d)
    data["_dir"] = str(d)
    data["_mtime"] = int((d / "project.json").stat().st_mtime * 1000)
    return data


@app.get("/api/project/{pid}/mtime")
def project_mtime(pid: str):
    d = pdir(pid)
    return {"mtime": int((d / "project.json").stat().st_mtime * 1000)}


@app.put("/api/project/{pid}")
async def save_project(pid: str, request: Request):
    d = pdir(pid)
    body = await request.json()
    by = body.get("_by", "you")
    body = {k: v for k, v in body.items() if not k.startswith("_")}
    body["by"] = by
    write_project(d, body)
    snapshot(d, body, "Auto-save", by, dedupe_autosave=True)
    gen_thumb(d)
    return {"ok": True, "mtime": int((d / "project.json").stat().st_mtime * 1000)}


# ---------- history ----------

@app.get("/api/project/{pid}/history")
def history(pid: str):
    return hist_index(pdir(pid))[:100]


@app.post("/api/project/{pid}/snapshot")
async def make_snapshot(pid: str, request: Request):
    d = pdir(pid)
    body = await request.json()
    snapshot(d, read_project(d), body.get("label", "Snapshot"), body.get("author", "you"))
    return {"ok": True}


@app.post("/api/project/{pid}/restore")
async def restore(pid: str, request: Request):
    d = pdir(pid)
    body = await request.json()
    idx = hist_index(d)
    entry = next((e for e in idx if e["file"] == Path(body["file"]).name), None)
    f = hist_dir(d) / Path(body["file"]).name
    if not entry or not f.exists():
        raise HTTPException(404, "snapshot not found")
    snapshot(d, read_project(d), f'Before restoring “{entry["label"]}”', "you")
    data = json.loads(f.read_text())
    write_project(d, data)
    gen_thumb(d, force=True)
    return get_project(pid)


# ---------- media ----------

@app.post("/api/project/{pid}/media")
async def upload_media(pid: str, file: UploadFile):
    d = pdir(pid) / "media"
    d.mkdir(exist_ok=True)
    name = f"{int(time.time())}_{Path(file.filename).name}".replace(" ", "_")
    dest = d / name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"path": f"media/{name}"}


CHUNK = 1024 * 1024


@app.get("/file")
def serve_file(path: str, request: Request):
    q = safe_path(path)
    size = q.stat().st_size
    ctype = mimetypes.guess_type(str(q))[0] or "application/octet-stream"
    rng = request.headers.get("range")
    if rng:
        try:
            start_s, end_s = rng.replace("bytes=", "").split("-")
            start = int(start_s)
            end = min(int(end_s) if end_s else start + CHUNK * 4 - 1, size - 1)
        except Exception:
            start, end = 0, size - 1
        length = end - start + 1

        def it():
            with q.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    data = f.read(min(CHUNK, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data

        return StreamingResponse(it(), status_code=206, media_type=ctype, headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes", "Content-Length": str(length)})
    return StreamingResponse(q.open("rb"), media_type=ctype,
                             headers={"Content-Length": str(size), "Accept-Ranges": "bytes"})


# ---------- jobs ----------

@app.post("/api/project/{pid}/export")
async def export(pid: str, request: Request):
    body = await request.json()
    quality = body.get("quality", "final")
    d = pdir(pid)
    (d / "exports").mkdir(exist_ok=True)
    project = read_project(d)
    scale = 1.0 if quality == "final" else min(1.0, 720 / min(project["w"], project["h"]))
    out = d / "exports" / f"{slug(project.get('name', pid))}_{'final' if quality == 'final' else '720p'}.mp4"
    fps = float(project.get("fps") or 30)
    total = int(sum(c["out"] - c["in"] for c in project["clips"]) * fps)
    jid = run_job([sys.executable, str(RENDER / "renderer.py"), str(d), str(out),
                   "--scale", str(scale)], total_hint=max(1, total))
    return {"job": jid, "out": str(out), "filename": out.name}


TRANSCRIBE_LANGS = {"auto", "en", "pt", "de", "es"}


@app.post("/api/project/{pid}/transcribe")
async def transcribe(pid: str, request: Request):
    """Auto captions: transcribe the timeline voice and append karaoke groups."""
    body = await request.json()
    lang = body.get("lang", "auto")
    if lang not in TRANSCRIBE_LANGS:
        raise HTTPException(400, "unsupported language")
    d = pdir(pid)
    project = read_project(d)
    if not project.get("clips"):
        raise HTTPException(400, "no clips to transcribe")
    dur = sum(c["out"] - c["in"] for c in project["clips"])

    def after(job):
        snapshot(d, read_project(d), f"Transcribed captions ({lang})", "you")

    jid = run_job([sys.executable, str(RENDER / "transcribe.py"), str(d), "--lang", lang],
                  total_hint=max(1, int(dur)), on_done=after)
    return {"job": jid}


@app.post("/api/project/{pid}/transcribe_source")
async def transcribe_source(pid: str, request: Request):
    """Transcript for ONE source (source-time words) → media/transcript_<key>.json."""
    body = await request.json()
    d = pdir(pid)
    key = str(body["key"])
    if not re.fullmatch(r"[A-Za-z0-9_]+", key):
        raise HTTPException(400, "bad source key")
    project_media(d, body["path"])  # validate the media path stays in-project
    lang = body.get("lang", "auto")
    if lang not in TRANSCRIBE_LANGS:
        raise HTTPException(400, "unsupported language")
    out_rel = f"media/transcript_{key}.json"
    jid = run_job([sys.executable, str(RENDER / "transcribe_source.py"),
                   str(d), body["path"], out_rel, "--lang", lang])
    return {"job": jid, "path": out_rel}


PREWARM = {}                 # pid -> job id, so the UI can show it running
PREWARM_LOCK = threading.Lock()


def prewarm_transcript(d: Path, key="main"):
    """Transcribe a freshly imported source in the background, once.

    Skips silently when a fresh transcript already exists — re-importing the same
    video, or a project restored from elsewhere, must not pay for it twice."""
    if any(k == key for k, _p in source_transcripts(d)):
        return None
    try:
        src = (read_project(d).get("sources") or {}).get(key) or {}
        rel = src.get("path")
    except OSError:
        return None
    if not rel:
        return None
    out_rel = f"media/transcript_{key}.json"
    with PREWARM_LOCK:
        running = PREWARM.get(d.name)
        if running and JOBS.get(running, {}).get("status") == "running":
            return running
        jid = run_job([sys.executable, str(RENDER / "transcribe_source.py"),
                       str(d), rel, out_rel, "--lang", "auto"])
        PREWARM[d.name] = jid
    return jid


@app.get("/api/project/{pid}/prewarm")
def prewarm_status(pid: str):
    """Is the automatic transcription still running for this project?"""
    d = pdir(pid)
    fresh = any(k == "main" for k, _p in source_transcripts(d))
    jid = PREWARM.get(d.name)
    st = JOBS.get(jid or "", {})
    return {"ready": fresh, "running": st.get("status") == "running",
            "progress": st.get("progress"), "total": st.get("total")}


@app.get("/api/project/{pid}/transcript")
def get_transcript(pid: str, path: str):
    d = pdir(pid)
    f = project_media(d, path)
    return json.loads(f.read_text())


@app.post("/api/project/{pid}/strip")
async def make_strip(pid: str, request: Request):
    """Timeline filmstrip: 1 frame per `interval` seconds of the media, tiled
    into one wide JPEG. The timeline maps it with pure CSS background math."""
    body = await request.json()
    d = pdir(pid)
    src = project_media(d, body["path"])
    w0, h0, dur = probe(src)
    dur = max(0.5, dur or 0.5)
    # tile width follows the source aspect (scale=-2:108); the whole strip must
    # stay under mjpeg's 65500px hard limit, so the interval widens with
    # duration (a 16:9 source at 192px/frame caps at ~340 frames per strip)
    tile_w = int(round((w0 / h0) * 108)) if w0 and h0 else 62
    tile_w = max(2, tile_w + (tile_w % 2))
    max_frames = max(2, 65000 // tile_w)
    interval = max(1, math.ceil(dur / (max_frames - 1)))
    frames = max(1, int(dur / interval) + 1)
    (d / "media").mkdir(exist_ok=True)
    out = d / "media" / f"strip_{hashlib.sha1(body['path'].encode()).hexdigest()[:8]}.jpg"
    return media_job(
        f"strip:{pid}:{src}", out,
        lambda tmp: [FFMPEG, "-y", "-i", str(src),
                     "-vf", f"fps=1/{interval},scale=-2:108,tile={frames}x1",
                     "-frames:v", "1", "-q:v", "7", "-f", "image2", str(tmp)],
        {"path": f"media/{out.name}", "interval": interval, "frames": frames})


@app.post("/api/project/{pid}/wave")
async def make_wave(pid: str, request: Request):
    """Audio waveform PNG for the timeline lane."""
    body = await request.json()
    d = pdir(pid)
    src = project_media(d, body["path"])
    _, _, dur = probe(src)
    dur = max(0.5, dur or 0.5)
    w = min(4000, max(200, int(dur * 20)))
    (d / "media").mkdir(exist_ok=True)
    out = d / "media" / f"wave_{hashlib.sha1(body['path'].encode()).hexdigest()[:8]}.png"
    return media_job(
        f"wave:{pid}:{src}", out,
        lambda tmp: [FFMPEG, "-y", "-i", str(src), "-filter_complex",
                     f"aformat=channel_layouts=mono,showwavespic=s={w}x108:colors=#6BD48F",
                     "-frames:v", "1", "-f", "image2", str(tmp)],
        {"path": f"media/{out.name}", "duration": round(dur, 3)})


@app.post("/api/project/{pid}/srcwave")
async def make_srcwave(pid: str, request: Request):
    """Waveform PNG of a SOURCE's audio for the video track (REN-114, CapCut-style
    cyan wave under the filmstrip). Same CSS background math as the filmstrip:
    natural width = duration, so it follows trim/zoom with no regeneration."""
    body = await request.json()
    d = pdir(pid)
    src = project_media(d, body["path"])
    _, _, dur = probe(src)
    dur = max(0.5, dur or 0.5)
    # 20px per second was 7x too coarse at high zoom — the wave came out blocky
    # and low-res exactly where he needs to read it. The width is part of the
    # filename so an old, coarser file is replaced rather than reused.
    w = min(20000, max(400, int(dur * 60)))
    (d / "media").mkdir(exist_ok=True)
    sig = hashlib.sha1(f"{body['path']}@{w}".encode()).hexdigest()[:8]
    out = d / "media" / f"vwave_{sig}.png"
    if has_audio(src):
        # speechnorm (peak-levels speech without clipping to a solid block) +
        # cbrt scale (perceptual — quiet parts stay visible) so the wave FILLS
        # the band like CapCut instead of a thin flat line for low-level speech.
        wf = f"aformat=channel_layouts=mono,speechnorm=e=12.5,showwavespic=s={w}x100:colors=#5FD9C9:scale=cbrt"
        cmd = lambda tmp: [FFMPEG, "-y", "-i", str(src), "-filter_complex", wf,  # noqa: E731
                           "-frames:v", "1", "-f", "image2", str(tmp)]
    else:  # no audio track (screen caps, silent b-roll) → a flat/empty wave.
        # -t is an INPUT option here (before -i) so the infinite anullsrc ends and
        # showwavespic can finish; as an output option it would hang forever.
        wf = f"aformat=channel_layouts=mono,showwavespic=s={w}x100:colors=#5FD9C9"
        cmd = lambda tmp: [FFMPEG, "-y", "-t", f"{dur:.3f}", "-f", "lavfi",  # noqa: E731
                           "-i", "anullsrc=r=44100:cl=mono", "-filter_complex", wf,
                           "-frames:v", "1", "-f", "image2", str(tmp)]
    return media_job(f"vwave:{pid}:{src}", out, cmd,
                     {"path": f"media/{out.name}", "duration": round(dur, 3)})


@app.post("/api/project/{pid}/segbg")
async def segbg(pid: str, request: Request):
    body = await request.json()
    jid = run_job([sys.executable, str(RENDER / "segbg.py"), str(pdir(pid)), body["clipId"]])
    return {"job": jid}


@app.post("/api/project/{pid}/retouch")
async def retouch_preview(pid: str, request: Request):
    """Accurate face-retouch preview for one clip (proxy-res, real pipeline)."""
    body = await request.json()
    jid = run_job([sys.executable, str(RENDER / "retouchpreview.py"), str(pdir(pid)), body["clipId"]])
    return {"job": jid}


@app.post("/api/project/{pid}/facedetect")
async def facedetect(pid: str, request: Request):
    """Face track for a source (YuNet). Returns the cached track when it already
    exists so the retouch chip/mask are instant on re-activation."""
    body = await request.json()
    d = pdir(pid)
    src = project_media(d, body["path"])
    out = d / "cache" / f"facetrack_{hashlib.sha1(body['path'].encode()).hexdigest()[:10]}.json"
    rel = f"cache/{out.name}"
    if out.exists():
        try:
            return {"done": True, "path": rel, "track": json.loads(out.read_text())}
        except Exception:
            pass
    jid = run_job([sys.executable, str(RENDER / "facedetect.py"), str(d), body["path"]])
    return {"job": jid, "path": rel}


@app.get("/api/project/{pid}/facetrack")
def get_facetrack(pid: str, path: str):
    """Read a completed face track (client fetches it after the job finishes)."""
    d = pdir(pid)
    f = project_media(d, path)
    return json.loads(f.read_text())


@app.post("/api/project/{pid}/cutout")
async def cutout(pid: str, request: Request):
    """Remove background of an image already in the project (rembg)."""
    body = await request.json()
    d = pdir(pid)
    src = project_media(d, body["path"])
    from PIL import Image
    from rembg import remove
    (d / "media").mkdir(exist_ok=True)
    out = d / "media" / (Path(src).stem + ".cutout.png")
    remove(Image.open(src).convert("RGBA")).save(out)
    return {"path": f"media/{out.name}"}


@app.post("/api/project/{pid}/proxy")
async def make_proxy(pid: str, request: Request):
    body = await request.json()
    d = pdir(pid)
    src = project_media(d, body["path"])
    (d / "media").mkdir(exist_ok=True)
    out = d / "media" / f"proxy_{Path(src).stem}.mp4"
    w, h, _ = probe(src)
    # keyed by SOURCE: an import/take proxy job already transcoding this file
    # is joined (and its output path adopted) instead of a duplicate transcode
    return media_job(f"proxy:{pid}:{src}", out,
                     lambda tmp: proxy_cmd(src, tmp, w or 1080, h or 1920),
                     {"path": f"media/{out.name}"})


@app.get("/api/job/{jid}")
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "no such job")
    return JOBS[jid]


@app.post("/api/reveal")
async def reveal(request: Request):
    body = await request.json()
    q = safe_path(body["path"])
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(q)])
        return {"ok": True}
    return {"ok": False, "reason": "not available on this host"}


@app.post("/api/open")
async def open_file(request: Request):
    body = await request.json()
    q = safe_path(body["path"])
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(q)])
        return {"ok": True}
    return {"ok": False, "reason": "not available on this host"}


# ---------- Claude chat ----------

@app.get("/api/preferences")
def get_preferences():
    return {"text": read_prefs()}


@app.put("/api/preferences")
async def put_preferences(request: Request):
    body = await request.json()
    text = body.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")
    text = text.strip()
    with PREFS_LOCK:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # keep the previous version as a .bak so an accidental overwrite (a wipe,
        # or an auto-learned rule lost to a stale Save) is always recoverable
        try:
            if PREFS_FILE.exists():
                PREFS_FILE.with_name(PREFS_FILE.name + ".bak").write_text(PREFS_FILE.read_text())
        except OSError:
            pass
        PREFS_FILE.write_text(text + ("\n" if text else ""))
    return {"ok": True, "text": read_prefs()}


def read_presets():
    try:
        data = json.loads(PRESETS_FILE.read_text())
        return data if isinstance(data, list) else None
    except (OSError, ValueError):
        return None


def _atomic_write_text(path: Path, text: str):
    """Write via tmp + os.replace so a concurrent reader never sees a truncated
    file (a GET landing mid-write would otherwise reseed defaults over the save)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _backup_presets():
    """One level of undo before we overwrite the user's presets."""
    try:
        if PRESETS_FILE.exists():
            PRESETS_FILE.with_name(PRESETS_FILE.name + ".bak").write_text(PRESETS_FILE.read_text())
    except OSError:
        pass


def read_seeded() -> set:
    try:
        data = json.loads(PRESETS_SEEDED_FILE.read_text())
        return set(data.get("ids") or []) if isinstance(data, dict) else set()
    except (OSError, ValueError, TypeError):
        return set()


def read_shipped() -> dict:
    """Fingerprint of the built-in prompt we last handed this machine, per id."""
    try:
        data = json.loads(PRESETS_SEEDED_FILE.read_text())
        return dict(data.get("shipped") or {}) if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _fp(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def write_seeded(ids: set, shipped: dict = None):
    try:
        PRESETS_SEEDED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(PRESETS_SEEDED_FILE, json.dumps(
            {"ids": sorted(ids), "shipped": shipped or {}}, indent=1))
    except OSError:
        pass  # worst case the marker is rewritten next time; never break the GET


def presets_or_seed():
    p = read_presets()
    if p is None:
        with PRESETS_LOCK:
            p = read_presets()  # double-check under the lock before seeding
            if p is None:
                PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(PRESETS_FILE, json.dumps(DEFAULT_PRESETS, ensure_ascii=False, indent=1))
                write_seeded({d["id"] for d in DEFAULT_PRESETS})
                return [dict(d) for d in DEFAULT_PRESETS]  # copy: callers must not mutate the constant
    # migration (REN-127): older seeds lack requireScript; refresh an UNMODIFIED
    # old seed prompt (marker: the slow "transcribing the final cut" caption step)
    changed = False
    for pr in p:
        if pr.get("id") == "first-edit" and "requireScript" not in pr:
            pr["requireScript"] = True
            changed = True
            if "transcribing the final cut" in (pr.get("prompt") or ""):
                pr["prompt"] = FIRST_EDIT_PROMPT
    # A default added after this file was written still has to arrive. Offer each
    # one ONCE and remember we did: renamed/edited (same id) is left untouched,
    # and deleted stays deleted instead of coming back on the next app load.
    seeded = read_seeded() | PRE_MARKER_PRESETS
    have = {pr.get("id") for pr in p if isinstance(pr, dict)}
    offered = set()
    for dflt in DEFAULT_PRESETS:
        if dflt["id"] in seeded:
            continue
        offered.add(dflt["id"])
        if dflt["id"] not in have:
            p.append(dict(dflt))
            changed = True
    # Same one-shot deal for the hover description, which built-ins predating it
    # don't carry. Only onto a preset whose prompt is still ours — a rewritten one
    # must not advertise what it no longer does.
    by_id = {d["id"]: d for d in DEFAULT_PRESETS}
    for pr in p:
        dflt = by_id.get(pr.get("id")) if isinstance(pr, dict) else None
        if not dflt or pr.get("desc") or pr.get("prompt") != dflt["prompt"]:
            continue
        key = "desc:" + dflt["id"]
        if key not in seeded:
            pr["desc"] = dflt["desc"]
            offered.add(key)
            changed = True
    # A built-in we CHANGED still has to reach a machine that already has the old
    # one — the id is already in `seeded`, so the "offer once" path above will
    # never look at it again. Refresh it only while it is provably still ours:
    # we remember the fingerprint of the prompt we handed over, so a preset he
    # has since rewritten is left exactly as he wrote it.
    #
    # Without this, changing how a built-in works ships the new prompt to new
    # installs and leaves every existing one quietly running the old behaviour.
    shipped = read_shipped()
    for pr in p:
        dflt = by_id.get(pr.get("id")) if isinstance(pr, dict) else None
        if not dflt:
            continue
        cur_fp = _fp(pr.get("prompt"))
        if cur_fp == _fp(dflt["prompt"]):
            shipped[dflt["id"]] = cur_fp          # already current; remember it
        elif shipped.get(dflt["id"]) == cur_fp:   # unmodified copy of an older one
            pr["prompt"] = dflt["prompt"]
            pr["requireScript"] = dflt.get("requireScript", True)
            if dflt.get("desc"):
                pr["desc"] = dflt["desc"]
            shipped[dflt["id"]] = _fp(dflt["prompt"])
            changed = True
        # the pipeline key is not cosmetic — without it the clean pass falls back
        # to asking the model, which is the thing it exists to avoid
        if dflt.get("pipeline") and pr.get("pipeline") != dflt["pipeline"] \
                and pr.get("prompt") == dflt["prompt"]:
            pr["pipeline"] = dflt["pipeline"]
            changed = True
    if changed:
        with PRESETS_LOCK:
            _backup_presets()
            _atomic_write_text(PRESETS_FILE, json.dumps(p, ensure_ascii=False, indent=1))
    if offered or shipped != read_shipped():
        # after the presets write: a crash in between re-offers, and the id is
        # already in the file by then, so nothing is appended twice
        write_seeded(read_seeded() | offered, shipped)
    return p


# Outside the repo: an update stashes the working tree, and a log living inside
# it gets swept away mid-write. Must match LOG in scripts/update.sh.
UPDATE_LOG = Path.home() / "Library" / "Logs" / "AIVideoStudio-update.log"


@app.get("/api/update/check")
def update_check():
    """Is there a newer version published? Compares this checkout against the
    remote WITHOUT touching the working tree (fetch only, never pull)."""
    def git(*a, timeout=25):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True,
                              text=True, timeout=timeout)
    try:
        if not (ROOT / ".git").exists():
            return {"supported": False, "reason": "not a git checkout"}
        if not (git("remote").stdout or "").strip():
            return {"supported": False, "reason": "no remote configured"}
        git("fetch", "--quiet", timeout=60)
        branch = (git("rev-parse", "--abbrev-ref", "HEAD").stdout or "main").strip()
        cnt = git("rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip()
        behind = int(cnt) if cnt.isdigit() else 0
        subject = ""
        if behind:
            subject = (git("log", "-1", "--format=%s", f"origin/{branch}").stdout or "").strip()
        return {"supported": True, "behind": behind, "latest": subject,
                "current": (git("log", "-1", "--format=%s").stdout or "").strip()}
    except Exception as e:  # noqa: BLE001 — never let this break the header
        return {"supported": False, "reason": str(e)[:120]}


@app.post("/api/update")
def update_run():
    """Start the update. Detached on purpose: its final step restarts THIS
    server, so an update running inside it would be killed halfway."""
    script = ROOT / "scripts" / "update.sh"
    if not script.exists():
        raise HTTPException(400, "update script missing")
    try:
        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_LOG.write_text("STEP starting\n")
    except OSError:
        pass
    subprocess.Popen(["/bin/bash", str(script)], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"started": True}


@app.get("/api/update/log")
def update_log():
    """Progress of a running update — the app polls this and keeps polling
    across the restart at the end."""
    try:
        txt = UPDATE_LOG.read_text()[-4000:]
    except OSError:
        return {"lines": [], "done": None}
    lines = [l for l in txt.splitlines() if l.strip()]
    done = None
    for l in lines:
        if l.startswith("DONE "):
            done = l.split(" ", 1)[1].strip()
    return {"lines": lines[-12:], "done": done}


@app.get("/api/version")
def get_version():
    """Build id of the served UI (dist mtime). The client polls this and
    auto-reloads when it changes — a stale cached UI hid the approve button
    for days (REN-131)."""
    try:
        return {"build": int((ROOT / "web" / "dist" / "index.html").stat().st_mtime)}
    except OSError:
        return {"build": 0}


@app.get("/api/engines")
def get_engines():
    """Which AI engines are usable on this machine (REN-130). claude = CLI on
    PATH (auth via setup-token/session); codex = CLI + ChatGPT login done."""
    return {"claude": engine_ready("claude"), "codex": engine_ready("codex")}


def _agent_env(engine):
    return codex_env() if engine == "codex" else claude_env()


@app.get("/api/models")
def get_models():
    """Models the picker offers: curated baseline for every installed engine,
    plus any model DISCOVERED by scanning the CLIs and probing the account
    (REN-136). Never blocks — a stale cache serves the baseline and refreshes in
    the background, so a new Claude/GPT shows up on its own."""
    engines = {"claude": engine_ready("claude"), "codex": engine_ready("codex")}
    try:
        models = modelscan.available(_agent_env, engines)
    except Exception:  # discovery must never break the picker
        models = []
    if not models:
        models = [{"key": f"{m['engine']}:{m['id']}", "engine": m["engine"],
                   "model": m["id"], "label": m["label"], "source": "baseline"}
                  for m in modelscan.BASELINE if engines.get(m["engine"])]
    return {"models": models, "engines": engines, "default": CHAT_DEFAULT_MODEL}


@app.post("/api/models/rescan")
def rescan_models():
    """Force a fresh scan+probe now (background)."""
    engines = {"claude": engine_ready("claude"), "codex": engine_ready("codex")}
    threading.Thread(target=modelscan.refresh, args=(_agent_env, engines),
                     daemon=True).start()
    return {"ok": True}


@app.get("/api/presets")
def get_presets():
    return {"presets": presets_or_seed()}


@app.put("/api/presets")
async def put_presets(request: Request):
    body = await request.json()
    presets = body.get("presets") if isinstance(body, dict) else None
    if not isinstance(presets, list):
        raise HTTPException(400, "presets must be a list")
    clean = []
    for p in presets:
        if not isinstance(p, dict):
            continue
        prompt = str(p.get("prompt") or "").strip()
        if not prompt:
            continue
        item = {
            "id": str(p.get("id") or ("p" + uuid.uuid4().hex[:8])),
            "name": (str(p.get("name") or "").strip() or "Untitled")[:80],
            "prompt": prompt,
            "requireScript": bool(p.get("requireScript")),
        }
        desc = str(p.get("desc") or "").strip()[:160]  # one sentence, shown on hover
        if desc:
            item["desc"] = desc
        # Editing a preset in the UI must not quietly turn a deterministic pass
        # back into an AI prompt, so the pipeline key survives the round trip.
        if p.get("pipeline") in ("cleanup",):
            item["pipeline"] = p["pipeline"]
        clean.append(item)
    with PRESETS_LOCK:
        PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _backup_presets()
        _atomic_write_text(PRESETS_FILE, json.dumps(clean, ensure_ascii=False, indent=1))
    return {"ok": True, "presets": clean}


def chat_file(d: Path) -> Path:
    return d / "chat.json"


def chat_read(d: Path):
    f = chat_file(d)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return []
    return []


def chat_append(d: Path, who: str, text: str):
    with CHAT_LOCK:
        msgs = chat_read(d)
        msgs.append({"who": who, "text": text, "ts": time.time()})
        chat_file(d).write_text(json.dumps(msgs[-200:], ensure_ascii=False, indent=1))


@app.get("/api/project/{pid}/chat")
def get_chat(pid: str):
    return chat_read(pdir(pid))


@app.post("/api/project/{pid}/chat")
async def post_chat(pid: str, request: Request):
    """Run Claude headless; it edits project.json directly. Returns a job id —
    poll /api/job/{jid}; result is the reply text."""
    d = pdir(pid)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "empty message")
    m, e = body.get("model"), body.get("effort")
    model_key, _spec = resolve_model(m)
    effort = e if isinstance(e, str) and e in CHAT_EFFORT_TOKENS else CHAT_DEFAULT_EFFORT
    running = CHAT_RUNNING.get(pid)
    if running and JOBS.get(running, {}).get("status") == "running":
        # A previous edit is still in flight. Don't error — hand the client the
        # running job so it can re-attach (show "working…") instead of a scary
        # 409, and keep the new message in the composer for when it's free.
        return {"job": running, "busy": True}
    preset = body.get("preset")
    preset = preset if isinstance(preset, str) else None
    chat_append(d, "you", message)
    return {"job": _launch_chat_job(pid, d, message, model_key, effort, preset)}


def preset_by_id(pid: str):
    """The preset a click came from, or None. Its own declared rules win over the
    heuristics: a first pass on RAW footage has nothing to write a script about."""
    if not pid:
        return None
    try:
        return next((p for p in presets_or_seed() if p.get("id") == pid), None)
    except Exception:  # noqa: BLE001 — a broken presets file must not block chat
        return None


def _launch_chat_job(pid: str, d: Path, message: str, model_key: str, effort: str,
                     preset_id: str = None) -> str:
    """Build the right prompt (script step vs edit) and spawn the chat worker.
    Also called by approve_script to auto-resume the parked request (REN-129)."""
    pj = d / "project.json"
    try:
        proj_now = read_project(d)
    except OSError:
        proj_now = {}
    # only FRESH transcripts are advertised (a stale one = the wrong video's words)
    fresh_tx = source_transcripts(d)
    tx_lines = [f'  - source "{k}" → {p}' for k, p in fresh_tx]
    proj_script = (proj_now.get("script") or "").strip()[:8000]  # cap: goes into argv
    script_approved = bool(proj_now.get("scriptApproved")) and bool(proj_script)
    tx_note = ("\nThese sources ALREADY have a word-level transcript on disk — READ it, do NOT "
               "re-transcribe (whisper is slow):\n" + "\n".join(tx_lines) +
               "\nOnly run transcribe_source.py for a source that has no (fresh) transcript.\n"
               ) if tx_lines else ""

    # ---- UNIVERSAL script gate (REN-129): EVERY edit request needs an approved
    # script first. Without one, step 1 writes the draft and PARKS the request;
    # approving in the Script tab auto-resumes it — no re-click, no user error.
    # A source whose fresh transcript has ZERO words (no speech) skips the gate —
    # there is nothing to script, and an empty script could never be approved.
    has_speech = None
    if fresh_tx:
        has_speech = False
        for _k, tp in fresh_tx:
            try:
                if json.loads(Path(tp).read_text()).get("words"):
                    has_speech = True
                    break
            except (OSError, ValueError):
                has_speech = True  # unreadable → assume speech, keep the gate
                break
    script_step = bool(proj_now.get("sources")) and not script_approved and has_speech is not False
    # A preset may opt out of the gate by declaring requireScript: false. Both
    # built-ins keep it: the approved script is what tells a first pass whether an
    # attempt finished the line or was abandoned.
    preset = preset_by_id(preset_id)
    if preset is not None and preset.get("requireScript") is False:
        script_step = False

    if script_step and proj_script:
        # a draft already awaits review — no AI run needed: remind + park
        park_edit(d, message, model_key, effort, preset_id)
        jid = uuid.uuid4().hex[:10]
        reply = ("O roteiro está na aba Script aguardando sua revisão — edite o que precisar e "
                 "clique “✓ Aprovar roteiro”. Assim que você aprovar, eu continuo este pedido "
                 "automaticamente.")
        JOBS[jid] = {"status": "done", "progress": 1, "total": 1, "log": [],
                     "result": reply, "started": time.time()}
        chat_append(d, "claude", reply)
        return jid

    if script_step:
        park_edit(d, message, model_key, effort, preset_id)
        prompt = f"""You are the AI video editor behind AI Video Studio. Before any editing, the workflow requires a SCRIPT the creator reviews and approves. Your ONLY job right now is to produce that script draft.

1. Ensure a word-level transcript of each source exists (source time). Run if missing:
  .venv/bin/python render/transcribe_source.py "{d}" <source_rel_path> media/transcript_<srcKey>.json --lang pt
{tx_note}
2. Read the transcript(s) in order and write the INTENDED script — the best/final version of each line, in the order meant. When a line was re-recorded, use the wording of the LAST complete correct take. Drop stumbles, false starts, fillers, repeated attempts.
3. Edit {pj}: set the root "script" field to that text (one clean line per sentence/thought) and set "scriptApproved" to false. Do NOT touch clips, captions, or anything else. Keep valid JSON; set "by" to "claude"; re-read to confirm it parses.

Reply in the user's language, 2-3 sentences: the script is ready in the Script tab — review it, fix anything (wording, numbers, missing lines) and click "✓ Aprovar roteiro"; the edit will then CONTINUE AUTOMATICALLY (they don't need to re-send anything)."""
    else:
        prompt = f"""You are the AI video editor behind AI Video Studio. The user is fine-tuning a video edit and asked, via the in-app chat:

"{message}"

Apply the request by editing this file directly: {pj}
Schema reference: {ROOT / 'SCHEMA.md'} (project.json schema v2 — x/y/size in percent, caption words in timeline seconds, clips concatenated).

You CAN run shell commands in this repo (working dir = {ROOT}); use .venv/bin/python for Python scripts (they need the repo's deps). Reuse the repo's own scripts instead of reinventing transcription/rendering.
Tasks that depend on the spoken words — choosing between repeated takes, cutting silence BETWEEN takes, syncing captions — need the transcript FIRST. Transcribe the source, then read the JSON:
  .venv/bin/python render/transcribe_source.py "{d}" <source_rel_path> media/transcript_<srcKey>.json --lang pt
{tx_note}It has word-level SOURCE-time timing. Use it to set each clip's in/out (never cut mid-sentence; trim only the gaps between takes). Work efficiently — you have a time budget; don't redo slow steps you've already done.
CAPTIONS: to (re)generate captions for the current cut, run:
  .venv/bin/python render/captions_from_transcript.py "{d}"
It builds word-synced source-anchored captions straight from the transcript — instant and exactly in sync. NEVER re-transcribe the cut for captions.

TAKE SELECTION (this is where edits go wrong — be precise):
- For a phrase said multiple times, keep the take spoken CORRECTLY and fully to the end; if several are complete, keep the LAST one.
- Set each clip's IN point to the FIRST WORD of the CORRECT version. If the SAME take has a false start, a stumble, or a botched attempt right before the good version, the clip must START on the good version — NEVER include the preceding error. Use that good word's transcript t0 as clip.in.
- MANDATORY self-check before you finish: for EVERY clip, read the transcript words inside [in, out] and confirm the opening words are the clean start of the intended phrase — not a repeated/stuttered/error fragment that is re-said correctly right after. If a clip opens on an error, move clip.in forward to the good word. State in your reply that you did this pass.

Rules:
- Edit {pj} in place; keep it valid schema-v2 JSON; set the root "by" field to "claude". Re-read it afterward to confirm it parses.
- Do not touch any other project.
- If the request is ambiguous, make the most reasonable choice and say what you did.
Reply to the user in 1-3 short sentences (plain text, same language as the user's message). Your reply is shown in the chat bubble."""

    prefs = read_prefs()
    if prefs and not script_step:
        prompt += ("\n\nRenato's standing editing preferences (learned from past videos — "
                   f"follow them unless this message overrides):\n{prefs}")

    if proj_script and not script_step:
        if script_approved:
            prompt += ("\n\nThe creator REVIEWED AND APPROVED this script — it is the SINGLE "
                       "SOURCE OF TRUTH for the edit:\n" + proj_script + "\n\n"
                       "Hard rules WHEN the request involves choosing/cutting takes (a first "
                       "edit or re-edit; small tweaks like caption style don't retrigger them):\n"
                       "- Every script line appears in the timeline EXACTLY ONCE — nothing from the "
                       "script missing, nothing kept that isn't in it.\n"
                       "- If two takes say slightly DIFFERENT wordings of the same part, the script "
                       "decides: keep ONLY the take whose wording matches the script (or the closest "
                       "one; if tied, the LAST complete take). Never keep both.\n"
                       "- Final self-check: walk the script line by line against the timeline and "
                       "confirm the one-to-one match before replying.")
        else:
            prompt += ("\n\nThe creator's draft INTENDED SCRIPT (not yet approved) — use as a hint "
                       f"for which lines/takes to keep:\n{proj_script}")

    # (skipped on the script step — that pass only reads a transcript and writes
    # text; the exhaustive re-read directive would just make it slower, REN-133)
    if effort == "ultracode" and not script_step:
        prompt += ("\n\nUltracode mode — work exhaustively: reason through every take and "
                   "segment before editing, then RE-READ the file to confirm it is valid "
                   "schema-v2 JSON and genuinely satisfies the request. Fix anything off "
                   "before replying.")

    if not script_step:
        pop_pending(d)  # this edit supersedes any stale parked request

    # A FIRST CUT on an untouched project with an approved script goes through
    # the deterministic take pipeline instead of letting the model write
    # timestamps — that is what produced mid-word cuts and rehearsal takes.
    # It must only fire when the request IS "make the cut": on an untouched
    # project "deixa a legenda maior" would otherwise be answered with a full
    # re-edit the user never asked for (REN-135 review).
    # A preset can name the deterministic pipeline it IS, instead of describing it
    # in prose and hoping the model runs the right tool.
    clean_pipeline = (bool(preset) and preset.get("pipeline") == "cleanup"
                      and (not script_step) and script_approved)
    take_pipeline = ((not clean_pipeline) and (not script_step) and script_approved
                     and _is_unedited(proj_now) and wants_first_cut(message))

    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "progress": 0, "total": 1, "log": [],
                 "result": None, "started": time.time()}
    CHAT_RUNNING[pid] = jid
    mtime_before = (d / "project.json").stat().st_mtime
    label = message if len(message) <= 34 else message[:34] + "…"

    def clips_sig():
        def num(v):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return 0.0
        try:
            return [(c.get("src", "main"), num(c.get("in", 0)), num(c.get("out", 0)))
                    for c in read_project(d).get("clips", [])]
        except Exception:  # bad/half-written JSON must not wedge the chat
            return []
    clips_before = clips_sig()

    def snapshot_partial_edits(reason):
        """A failed run may still have edited the file — keep it restorable."""
        try:
            if (d / "project.json").stat().st_mtime != mtime_before:
                snapshot(d, read_project(d), f"Chat ({reason}): “{label}”", "claude")
        except Exception:
            pass

    def work():
        try:
            if clean_pipeline:
                out = run_cleanup_pipeline(d)
                if out:
                    try:
                        snapshot(d, read_project(d), f"Clean pass: “{label}”", "claude")
                    except Exception:
                        pass
                    chat_append(d, "claude", out)
                    gen_thumb(d)
                    JOBS[jid].update({"result": out, "progress": 1, "status": "done"})
                    return
                chat_append(d, "claude", "ℹ️ The clean pass could not run here — falling "
                                         "back to the AI; check the result.")
            if take_pipeline:
                out = run_take_pipeline(pid, d, model_key, effort)
                if out:
                    try:
                        snapshot(d, read_project(d), f"First cut: “{label}”", "claude")
                    except Exception:
                        pass
                    chat_append(d, "claude", out)
                    gen_thumb(d)
                    JOBS[jid].update({"result": out, "progress": 1, "status": "done"})
                    return
                # pipeline declined (no picks / no takes) → free-form fallback,
                # but SAY so: silently dropping to the old path is how a bad cut
                # would look like a normal result again (REN-135 review)
                chat_append(d, "claude", "ℹ️ The deterministic cut could not run this time — "
                                         "falling back to the older path; check the result.")
            # full tool access either engine (claude -p / codex exec); the user
            # opted into bypassed approvals for this local single-user app.
            r = run_agent(prompt, model_key, effort, 1800)
            reply = (r.stdout or "").strip()
            if r.returncode != 0 or not reply:
                err = redact_secrets((r.stderr or reply or "the AI CLI failed").strip()[-500:])
                err = engine_auth_error(model_key, err) or err
                JOBS[jid]["status"] = "error"
                JOBS[jid]["log"] = [err]
                snapshot_partial_edits("failed")
                chat_append(d, "claude", f"⚠ I couldn't complete that: {err[:220]}")
                return
            if script_step:
                # re-assert the AI's script over any client autosave that landed
                # between its write and now (lost-update guard, REN-128 review)
                try:
                    ai_script = (read_project(d).get("script") or "").strip()
                    if ai_script:
                        time.sleep(1.2)  # let a just-in-flight client PUT land
                        fresh = read_project(d)
                        if (fresh.get("script") or "").strip() != ai_script:
                            fresh["script"] = ai_script
                            fresh["scriptApproved"] = False
                            write_project(d, fresh)
                except Exception:
                    pass
            try:
                snapshot(d, read_project(d), f"Chat revision: “{label}”", "claude")
            except Exception:
                pass
            chat_append(d, "claude", reply)
            gen_thumb(d)
            # third-layer auditor: if this edit changed the take selection, run a
            # focused pass that trims any clip opening on a false start. Do it
            # BEFORE marking done so the client reloads the audited result in one
            # shot. Guarded — an auditor hiccup must not fail the main edit.
            # Never after a script step (it must not touch clips; any clips_sig
            # delta then is the USER editing during the run — don't "audit" that).
            try:
                if not script_step and clips_sig() != clips_before:
                    pre_audit = (d / "project.json").read_bytes()
                    fixed = audit_openings(d, model_key)
                    # the auditor edits project.json with bypassPermissions —
                    # confirm it left valid JSON; roll back to the pre-audit bytes
                    # if it corrupted the file (never ship a broken project).
                    try:
                        read_project(d)
                        valid = True
                    except Exception:
                        valid = False
                    if not valid:
                        (d / "project.json").write_bytes(pre_audit)
                        chat_append(d, "claude", "⚠ Auditor left the file invalid — reverted the audit pass (your edit is intact).")
                    elif fixed and "already clean" not in fixed.lower():
                        snapshot(d, read_project(d), f"Audit openings: “{label}”", "claude")
                        gen_thumb(d)
                        chat_append(d, "claude", f"🔎 {fixed}")
            except Exception:
                pass
            # CAPTIONS ARE ALWAYS THE LAST STEP (REN-138). The prompt tells the
            # model to regenerate them, but "tells" is not a guarantee: whenever
            # it re-cut and skipped that, the timeline shipped captions built for
            # the PREVIOUS cut, which is what "a legenda é feita antes da edição"
            # actually was. Run it here so the cut can never outrun the caption.
            try:
                if not script_step and clips_sig() != clips_before:
                    cap = subprocess.run(
                        [sys.executable, str(ROOT / "render" / "captions_from_transcript.py"), str(d)],
                        capture_output=True, text=True, timeout=300)
                    if cap.returncode == 0:
                        gen_thumb(d)
                    else:
                        chat_append(d, "claude", "⚠ I could not rebuild the captions for this cut — "
                                                 "the ones on the timeline may be stale.")
            except Exception:
                pass
            JOBS[jid]["result"] = reply
            JOBS[jid]["progress"] = 1
            JOBS[jid]["status"] = "done"
            # learn a durable preference from this edit (background, best-effort).
            # Guard the spawn so a thread failure can't flip this DONE job to error.
            # Skip script-step runs — preset boilerplate + canned notice teach nothing.
            if not script_step:
                try:
                    threading.Thread(target=learn_pref, args=(message, reply, model_key), daemon=True).start()
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"] = ["The AI engine timed out after 30 minutes"]
            snapshot_partial_edits("timed out")
            chat_append(d, "claude", "⚠ I timed out working on that — try a smaller request.")
        except Exception as e:  # noqa: BLE001
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"] = [str(e)]
            snapshot_partial_edits("failed")
            chat_append(d, "claude", f"⚠ Something broke on my side: {e}")
        finally:
            if CHAT_RUNNING.get(pid) == jid:
                CHAT_RUNNING.pop(pid, None)
            # the user may have approved WHILE the script step ran (the draft
            # appears in the tab seconds before the job ends) — approve_script
            # then returned busy without resuming. Resume here so "continuo
            # automaticamente" is always true (REN-129 review).
            if script_step:
                try:
                    fresh = read_project(d)
                    if fresh.get("scriptApproved") and (fresh.get("script") or "").strip():
                        pend = pop_pending(d)
                        if pend:
                            chat_append(d, "claude", "✅ Script approved — carrying on with the edit…")
                            _launch_chat_job(pid, d, pend["message"],
                                             pend.get("model") or model_key,
                                             pend.get("effort") or effort,
                                             pend.get("preset"))
                except Exception:
                    pass
            _mark_chat_idle(d, jid)

    _mark_chat_running(d, jid, label)
    threading.Thread(target=work, daemon=True).start()
    return jid


# A chat job lives in memory, so a restart erases every trace of it and the tab
# polling it just gets a 404. That is not exotic: the Update button restarts the
# server on purpose. Leave a breadcrumb on disk so the next boot can say what
# happened instead of letting an edit die in silence.
def _running_file(d: Path) -> Path:
    return d / ".chat_running.json"


def _mark_chat_running(d: Path, jid: str, label: str):
    try:
        _running_file(d).write_text(json.dumps(
            {"job": jid, "label": label, "at": time.time()}, ensure_ascii=False))
    except OSError:
        pass


def _mark_chat_idle(d: Path, jid: str = None):
    """Clear the breadcrumb — but only if it is still OURS. The script step hands
    straight over to the real edit inside its own finally block, so clearing
    blindly erases the marker the job that just started had already written."""
    try:
        if jid is not None:
            try:
                if json.loads(_running_file(d).read_text()).get("job") != jid:
                    return
            except (OSError, ValueError):
                return
        _running_file(d).unlink()
    except OSError:
        pass


def report_interrupted_chats():
    """Called once at startup: tell each project whose edit we killed."""
    try:
        dirs = [p for p in PROJECTS.iterdir() if p.is_dir()]
    except OSError:
        return
    for d in dirs:
        f = _running_file(d)
        if not f.exists():
            continue
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            info = {}
        _mark_chat_idle(d)
        what = f" (“{info.get('label')}”)" if info.get("label") else ""
        try:
            chat_append(d, "claude",
                        f"⚠ The server restarted while I was working on this{what}, so that "
                        "run stopped where it was. Nothing was damaged — send it again when "
                        "you are ready.")
        except Exception:  # noqa: BLE001 — a broken chat file must not block boot
            pass


@app.post("/api/project/{pid}/approve_script")
async def approve_script(pid: str):
    """Approve the script SERVER-SIDE (immune to the client autosave debounce)
    and auto-resume the parked edit request, if any (REN-129 single-flow)."""
    d = pdir(pid)
    cur = read_project(d)
    if not (cur.get("script") or "").strip():
        raise HTTPException(400, "no script to approve")
    cur["scriptApproved"] = True
    write_project(d, cur)
    running = CHAT_RUNNING.get(pid)
    if running and JOBS.get(running, {}).get("status") == "running":
        # a job is mid-flight; its finally-block resumes the parked edit once it
        # ends (the client re-checks after attaching to this one)
        return {"job": running, "busy": True, "approved": True}
    pend = pop_pending(d)
    if pend:
        chat_append(d, "claude", "✅ Script approved — carrying on with the edit…")
        jid = _launch_chat_job(pid, d, pend["message"],
                               pend.get("model") or CHAT_DEFAULT_MODEL,
                               pend.get("effort") or CHAT_DEFAULT_EFFORT,
                               pend.get("preset"))
        return {"job": jid, "approved": True}
    return {"job": None, "approved": True}


@app.post("/api/project/{pid}/generate_script")
async def generate_script(pid: str, request: Request):
    """Deduce the CLEAN intended script from the source transcript(s) and store it
    on project['script']. Returns a job id (poll /api/job) like chat."""
    d = pdir(pid)
    body = await request.json()
    m = body.get("model") if isinstance(body, dict) else None
    model_key, _spec = resolve_model(m)
    tx = source_transcripts(d)
    if not tx:
        raise HTTPException(400, "Transcribe the video first (Cut tab) so I can read the words.")
    # serialize with chat edits (both write project.json) — don't run concurrently
    running = CHAT_RUNNING.get(pid)
    if running and JOBS.get(running, {}).get("status") == "running":
        raise HTTPException(409, "Claude is already working on this project — wait for the current request")
    tx_ref = "\n".join(f'  - source "{k}": {p}' for k, p in tx)
    sp = (f"""Read the word-level source transcript(s) of this raw recording and write the CLEAN intended SCRIPT — the best/final version of what the creator meant to say, as if they said it perfectly once.

Transcripts (t0/t1 in SOURCE seconds):
{tx_ref}

Rules:
- One clean line per sentence/thought, in the order intended.
- When a line was re-recorded (repeated takes), use the wording of the LAST complete correct version.
- Drop stumbles, false starts, filler, and repeated attempts — keep only the intended words.
- No timestamps, no take labels, no commentary, no markdown. Output ONLY the script text, in the creator's own language.""")
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "progress": 0, "total": 1, "result": None, "log": [], "started": time.time()}
    CHAT_RUNNING[pid] = jid

    def work():
        try:
            r = run_agent(sp, model_key, "medium", 600)
            script = (r.stdout or "").strip()
            if r.returncode != 0 or not script:
                JOBS[jid]["status"] = "error"
                JOBS[jid]["log"] = [redact_secrets((r.stderr or "failed").strip()[-300:])]
                return
            cur = read_project(d)
            cur["script"] = script
            cur["scriptApproved"] = False  # a regenerated script must be re-reviewed
            write_project(d, cur)
            JOBS[jid]["result"] = script
            JOBS[jid]["progress"] = 1
            JOBS[jid]["status"] = "done"
        except subprocess.TimeoutExpired:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"] = ["timed out"]
        except Exception as e:  # noqa: BLE001
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"] = [str(e)]
        finally:
            if CHAT_RUNNING.get(pid) == jid:
                CHAT_RUNNING.pop(pid, None)

    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


@app.post("/api/project/{pid}/apply_script_to_captions")
async def apply_script_to_captions(pid: str, request: Request):
    """Correct the caption word TEXT to match the (edited) script — spelling,
    wording, number format — while KEEPING every word's timing. Job + lock +
    JSON-validate rollback, like the auditor."""
    d = pdir(pid)
    body = await request.json()
    m = body.get("model") if isinstance(body, dict) else None
    model_key, _spec = resolve_model(m)
    proj = read_project(d)
    script = (proj.get("script") or "").strip()
    if not script:
        raise HTTPException(400, "Generate or write the script first (Script tab).")
    if not proj.get("captions"):
        raise HTTPException(400, "No captions to correct — transcribe the video first (Cut tab).")
    running = CHAT_RUNNING.get(pid)
    if running and JOBS.get(running, {}).get("status") == "running":
        raise HTTPException(409, "Claude is already working on this project — wait for the current request")
    pj = d / "project.json"
    ap = f"""The creator corrected the SCRIPT of this video — the right words, spelling, and number format. Apply that to the on-screen CAPTIONS so the caption text matches the script, WITHOUT changing the timing/sync.

Corrected script:
{script[:16000]}

Edit the captions in {pj} (schema-v2; source-anchored karaoke groups; each word has "w" plus t0/t1 in SOURCE seconds):
- Rewrite ONLY the word text ("w") so each caption matches the script's wording, spelling, and number format (digits vs spelled-out) for that part of the video.
- KEEP every word's t0/t1 EXACTLY (the sync must not move). Keep the same groups and their order.
- If the corrected wording has a different number of words than the spoken ones in a group (e.g. a number written as digits vs said in full), keep it readable and re-distribute THAT group's t0/t1 across the new words — never change the group's overall start or end.
- Keep the creator's caption style: short karaoke lines, no sentence punctuation on the words.
- Keep {pj} valid schema-v2 JSON; set root "by" to "claude". Re-read it to confirm it parses. Do NOT touch clips or anything except captions.
Reply with ONE short line: how many caption groups you corrected."""
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "progress": 0, "total": 1, "result": None, "log": [], "started": time.time()}
    CHAT_RUNNING[pid] = jid

    def work():
        try:
            pre = pj.read_bytes()  # captured just before the AI edits (narrow window)
            r = run_agent(ap, model_key, "high", 900)
            reply = (r.stdout or "").strip()
            if r.returncode != 0:
                JOBS[jid]["status"] = "error"
                JOBS[jid]["log"] = [redact_secrets((r.stderr or "failed").strip()[-300:])]
                return
            # ENFORCE the promise mechanically (a prompt is not a guarantee): revert
            # unless clips are untouched, groups unchanged in id/count/order, and
            # each group's overall start/end (words[0].t0 / words[-1].t1) is
            # preserved. Within-group per-word redistribution IS allowed.
            def bad_revert(msg):
                pj.write_bytes(pre)
                JOBS[jid]["status"] = "error"
                JOBS[jid]["log"] = [msg]
            try:
                after = read_project(d)
                before = json.loads(pre)
            except Exception:
                bad_revert("left the file invalid — reverted")
                return
            bg, ag = before.get("captions") or [], after.get("captions") or []

            def bounds(g):
                ws = g.get("words") or []
                return ((ws[0].get("t0") if ws else None), (ws[-1].get("t1") if ws else None))
            ok = (
                after.get("clips") == before.get("clips")
                and len(ag) == len(bg)
                and [g.get("id") for g in ag] == [g.get("id") for g in bg]
                and all(bounds(a) == bounds(b) for a, b in zip(ag, bg))
            )
            if not ok:
                bad_revert("changed timing/clips/groups — reverted the caption pass")
                return
            snapshot(d, read_project(d), "Apply script to captions", "claude")
            gen_thumb(d)
            JOBS[jid]["result"] = reply or "captions corrected"
            JOBS[jid]["progress"] = 1
            JOBS[jid]["status"] = "done"
        except subprocess.TimeoutExpired:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"] = ["timed out"]
        except Exception as e:  # noqa: BLE001
            JOBS[jid]["status"] = "error"
            JOBS[jid]["log"] = [str(e)]
        finally:
            if CHAT_RUNNING.get(pid) == jid:
                CHAT_RUNNING.pop(pid, None)

    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


report_interrupted_chats()   # say what the last shutdown cut short, before serving

web_dist = ROOT / "web" / "dist"
if web_dist.exists():
    app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")
