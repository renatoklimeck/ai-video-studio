"""Model discovery (REN-136) — the picker keeps itself up to date.

Hardcoding model ids means the app silently ages: a new Claude or GPT ships and
the editor still offers last year's. Neither CLI has a "list models" command, so
this discovers them the two ways that actually work:

  1. SCAN the installed CLI binaries for model ids they know about. Updating the
     CLI is what brings new models, so this catches them for free.
  2. PROBE a candidate with a one-word prompt. A model the account cannot use
     answers `404 not_found_error`; a real one answers. That is the only honest
     test of "can this user actually run it".

Results are cached (with the CLI's own version as part of the key, so a CLI
update re-scans) and probing happens in the background — the picker never waits.
A curated baseline is always present, so the app works offline and on day one.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

CACHE = Path.home() / ".claude" / "video-studio-models.json"
TTL = 24 * 3600
PROBE_TIMEOUT = 120
MAX_PROBES = 8          # per refresh, so a scan never floods the subscription

# Known-good baseline. Anything here is offered even before a probe runs; the
# scan/probe only ADDS to it (or marks an entry unavailable).
BASELINE = [
    {"id": "claude-opus-5", "engine": "claude", "label": "Opus 5"},
    {"id": "claude-opus-4-8", "engine": "claude", "label": "Opus 4.8"},
    {"id": "claude-sonnet-5", "engine": "claude", "label": "Sonnet 5"},
    {"id": "claude-fable-5", "engine": "claude", "label": "Fable 5"},
    {"id": "claude-haiku-4-5-20251001", "engine": "claude", "label": "Haiku 4.5"},
    {"id": "", "engine": "codex", "label": "Codex (ChatGPT)"},
]
# Legacy UI keys (saved in localStorage / parked requests) → canonical ids
ALIASES = {
    "opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5",
    "fable": "claude-fable-5", "haiku": "claude-haiku-4-5-20251001",
    "codex": "codex:",
}

# Anchored so a capture can't run into the next string in the binary
# ("gpt-5.5openai.gpt…" wasted probes before the \b).
CLAUDE_RE = re.compile(r"\bclaude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+){0,2}\b")
CODEX_RE = re.compile(r"\bgpt-\d+(?:\.\d+)?(?:-codex)?(?:-(?:max|mini|nano))?\b")
_LOCK = threading.Lock()
_REFRESHING = False


def _bin(name):
    p = shutil.which(name)
    return Path(p).resolve() if p else None


def _cli_version(name):
    exe = _bin(name)
    if not exe:
        return None
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip()[:60]
    except Exception:
        return "?"


def scan_binary(engine):
    """Model ids the installed CLI itself knows about."""
    exe = _bin("claude" if engine == "claude" else "codex")
    if not exe:
        return []
    try:
        raw = exe.read_bytes()
    except OSError:
        return []
    text = raw.decode("utf-8", "ignore")
    rx = CLAUDE_RE if engine == "claude" else CODEX_RE
    out = set()
    for m in rx.findall(text):
        m = m.strip("-.")
        # drop dated/versioned duplicates of the same family and junk captures
        if engine == "claude" and re.search(r"-v\d$", m):
            continue
        if len(m) > 46:
            continue
        out.add(m)
    return sorted(out)


def probe(engine, model_id, env_for):
    """True if this account can actually run the model (404 = it cannot)."""
    if engine == "claude":
        exe = _bin("claude")
        if not exe:
            return False
        cmd = [str(exe), "-p", "Reply with exactly: OK", "--model", model_id]
    else:
        exe = _bin("codex")
        if not exe:
            return False
        cmd = [str(exe), "exec", "--skip-git-repo-check", "--ignore-user-config",
               "-s", "read-only"]
        if model_id:
            cmd += ["-m", model_id]
        cmd.append("Reply with exactly: OK")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT, env=env_for(engine),
                           stdin=subprocess.DEVNULL)
    except Exception:
        return False
    blob = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
    if "not_found_error" in blob or "404" in blob or "unknown model" in blob:
        return False
    return r.returncode == 0 and "ok" in blob


def pretty(model_id, engine):
    if engine == "codex":
        if not model_id:
            return "Codex (ChatGPT)"
        name = model_id.replace("gpt-", "GPT-")
        for suffix, nice in (("-codex", " Codex"), ("-mini", " Mini"),
                             ("-nano", " Nano"), ("-max", " Max")):
            name = name.replace(suffix, nice)
        return name
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?", model_id or "")
    if not m:
        return model_id
    fam, major, minor = m.group(1).capitalize(), m.group(2), m.group(3)
    return f"{fam} {major}.{minor}" if minor else f"{fam} {major}"


def _load():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save(data):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, CACHE)
    except OSError:
        pass


def refresh(env_for, engines_available):
    """Scan + probe (slow — always call from a background thread)."""
    global _REFRESHING
    with _LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True
    try:
        data = _load()
        known = {m["id"]: m for m in data.get("models", [])}
        candidates = []
        for eng in ("claude", "codex"):
            if not engines_available.get(eng):
                continue
            for mid in scan_binary(eng):
                key = f"{eng}:{mid}"
                if key not in known:
                    candidates.append((eng, mid, key))
        # newest-looking first, and never probe more than MAX_PROBES per refresh
        candidates.sort(key=lambda c: c[1], reverse=True)
        for eng, mid, key in candidates[:MAX_PROBES]:
            known[key] = {"id": key, "engine": eng, "model": mid,
                          "label": pretty(mid, eng), "ok": probe(eng, mid, env_for),
                          "checked": int(time.time())}
        _save({"ts": int(time.time()),
               "cli": {e: _cli_version(e) for e in ("claude", "codex")},
               "models": list(known.values())})
    finally:
        with _LOCK:
            _REFRESHING = False


def available(env_for, engines_available, background=True):
    """Picker list: baseline (for installed engines) + probed discoveries.
    Never blocks: a stale/missing cache just serves the baseline and kicks off a
    refresh."""
    data = _load()
    cli_now = {e: _cli_version(e) for e in ("claude", "codex")}
    stale = (time.time() - data.get("ts", 0) > TTL) or data.get("cli") != cli_now
    if stale and background:
        threading.Thread(target=refresh, args=(env_for, engines_available),
                         daemon=True).start()

    out, seen = [], set()
    for m in BASELINE:
        if not engines_available.get(m["engine"]):
            continue
        key = f"{m['engine']}:{m['id']}"
        out.append({"key": key, "engine": m["engine"], "model": m["id"],
                    "label": m["label"], "source": "baseline"})
        seen.add(key)
    for m in data.get("models", []):
        if not m.get("ok") or m["id"] in seen or not engines_available.get(m.get("engine")):
            continue
        seen.add(m["id"])
        out.append({"key": m["id"], "engine": m["engine"], "model": m.get("model"),
                    "label": m.get("label") or m["id"], "source": "discovered"})
    return out
