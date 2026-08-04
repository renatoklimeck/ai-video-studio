"""Auto captions: transcribe the TIMELINE voice and append karaoke caption
groups to project.json.

Pipeline: concat the clips' source audio (same cut math as the renderer) into
a 16kHz mono WAV → whisper-cli (whisper.cpp) with one-word segments → group
words into short caption lines (Renato's caption style: one short line at a
time, word-synced) → append to project.captions.

Usage: python transcribe.py <project_dir> [--lang auto|en|pt|de|es]
Prints "PROGRESS i/n" lines for the job runner and "done: N captions".

Model resolution: $VSTUDIO_WHISPER_MODEL, else the first existing of
~/loom-tools/models/ggml-small.bin (Mac) or ~/.models/ggml-small.bin (VPS).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import load_project, out_duration, out_to_source
from renderer import FFMPEG, resolve

MAX_CHARS = 23     # one short line at a time
MAX_GAP = 0.6      # silence that starts a new caption group
CAPTION_STYLE = {"x": 50, "y": 68, "size": 3.2, "font": "Helvetica Bold",
                 "color": "#FFFFFF", "dim": 0.45, "mode": "karaoke"}

# prefer the most accurate model present (large-v3 → medium → small); the app
# downloads large-v3 for reliable PT captions on takes with repetition/errors.
MODEL_CANDIDATES = [
    Path.home() / "loom-tools" / "models" / "ggml-large-v3.bin",
    Path.home() / "loom-tools" / "models" / "ggml-medium.bin",
    Path.home() / "loom-tools" / "models" / "ggml-small.bin",
    Path.home() / ".models" / "ggml-large-v3.bin",
    Path.home() / ".models" / "ggml-medium.bin",
    Path.home() / ".models" / "ggml-small.bin",
]


def find_model() -> Path:
    envp = os.environ.get("VSTUDIO_WHISPER_MODEL")
    if envp and Path(envp).exists():
        return Path(envp)
    for p in MODEL_CANDIDATES:
        if p.exists():
            return p
    sys.exit("whisper model not found — set VSTUDIO_WHISPER_MODEL or install ggml-small.bin")


VAD_CANDIDATES = [
    Path.home() / "loom-tools" / "models" / "ggml-silero-v5.1.2.bin",
    Path.home() / ".models" / "ggml-silero-v5.1.2.bin",
]
VAD_URL = ("https://huggingface.co/ggml-org/whisper-vad/resolve/main/"
           "ggml-silero-v5.1.2.bin?download=true")


def find_vad_model():
    """Silero VAD for whisper.cpp. Without it whisper HALLUCINATES words inside
    pauses — measured 30% of words landing in real silence on a raw take-heavy
    recording, which wrecked cut boundaries and caption sync. With VAD: 5%.
    It is <1MB, so fetch it once automatically (REN-134)."""
    envp = os.environ.get("VSTUDIO_VAD_MODEL")
    if envp and Path(envp).exists():
        return Path(envp)
    for p in VAD_CANDIDATES:
        if p.exists():
            return p
    dest = VAD_CANDIDATES[0]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        subprocess.run(["curl", "-sL", "--fail", "-o", str(tmp), VAD_URL],
                       check=True, timeout=180)
        tmp.replace(dest)
        return dest
    except Exception:
        return None


def build_voice_wav(project, pdir: Path, out_wav: Path):
    """Timeline voice = per-clip source audio, concatenated (no music/overlays)."""
    inputs, idx = [], {}

    def input_for(path):
        if path not in idx:
            idx[path] = len(inputs)
            inputs.append(path)
        return idx[path]

    parts, labels = [], []
    for i, c in enumerate(project["clips"]):
        src = project["sources"].get(c.get("src", "main")) or {}
        p = resolve(pdir, src.get("path"))
        if not p or not Path(p).is_file():
            sys.exit(f"clip {c['id']}: source not found ({p})")
        k = input_for(p)
        parts.append(f"[{k}:a]atrim=start={c['in']:.4f}:end={c['out']:.4f},asetpts=PTS-STARTPTS[s{i}];")
        labels.append(f"[s{i}]")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]")
    cmd = [FFMPEG, "-y", "-loglevel", "error"]
    for p in inputs:
        cmd += ["-i", p]
    cmd += ["-filter_complex", "".join(parts), "-map", "[out]",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"audio extraction failed: {r.stderr[-400:]}")


TS_RE = re.compile(r"\[(\d+):(\d+):(\d+)\.(\d+) -->")


def whisper_threads():
    """How many cores whisper may take.

    All of them minus one is right when it runs alone, and wrong when the server
    starts it alongside the import's 4K->720p transcode (REN-153): both then ask
    for most of the machine and thrash. The server sets
    VSTUDIO_WHISPER_THREADS to leave the transcode its share; on its own,
    whisper still gets everything but one core."""
    try:
        n = int(os.environ.get("VSTUDIO_WHISPER_THREADS") or 0)
    except ValueError:
        n = 0
    return max(2, n or ((os.cpu_count() or 4) - 1))


def transcribe(wav: Path, lang: str, dur: float, tmpdir: str, use_vad: bool = True,
               model: Path = None):
    """whisper-cli with one-word segments; returns [(word, t0, t1)].

    use_vad=False is for the assembler's EAR slices: with VAD + word segments
    whisper fuses an aborted attempt with its retake into one clean sentence
    even inside a 4-second slice, which is precisely what the ear exists to
    hear. Without VAD it reports the restart honestly ("Faz a conta… Faz a
    conta comigo."). The full-video pass keeps VAD, where it prevents invented
    words inside long pauses."""
    whisper = shutil.which("whisper-cli")
    if not whisper:
        sys.exit("whisper-cli not found on this host")
    base = Path(tmpdir) / "words"
    cmd = [whisper, "-m", str(model or find_model()), "-f", str(wav),
           "-l", lang, "-ml", "1", "-sow", "-oj", "-of", str(base),
           "-t", str(whisper_threads())]
    vad = find_vad_model() if use_vad else None
    if vad:
        # VAD keeps whisper from inventing words inside pauses; the word times
        # are what every cut and caption downstream is built on.
        cmd += ["--vad", "-vm", str(vad), "-vsd", "120", "-vp", "40"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    total = max(1, int(dur))
    for line in proc.stdout:
        m = TS_RE.search(line)
        if m:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            print(f"PROGRESS {min(t, total)}/{total}", flush=True)
    if proc.wait() != 0:
        sys.exit("whisper-cli failed")

    data = json.loads((base.with_suffix(".json")).read_text())
    words = []
    for seg in data.get("transcription", []):
        w = seg.get("text", "").strip()
        if not w or not re.search(r"[\wÀ-ɏ]", w):
            continue  # skip empty / punctuation-only / sound tokens
        if re.fullmatch(r"[\[\(].*[\]\)]", w):
            continue  # [Music], (laughs), …
        t0 = seg["offsets"]["from"] / 1000.0
        t1 = seg["offsets"]["to"] / 1000.0
        words.append((w, t0, max(t1, t0 + 0.05)))
    return words


def group_words(words):
    """Short caption lines: break on length or on a silence gap."""
    groups, cur = [], []
    for w, t0, t1 in words:
        if cur:
            line = " ".join(x[0] for x in cur)
            gap = t0 - cur[-1][2]
            if len(line) + 1 + len(w) > MAX_CHARS or gap > MAX_GAP:
                groups.append(cur)
                cur = []
        cur.append((w, t0, t1))
    if cur:
        groups.append(cur)
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--lang", default="auto")
    args = ap.parse_args()

    pdir = Path(args.project_dir)
    project = load_project(pdir / "project.json")
    if not project["clips"]:
        sys.exit("no clips to transcribe")
    dur = out_duration(project)

    tmpdir = tempfile.mkdtemp(prefix="vstudio_tr_")
    wav = Path(tmpdir) / "voice.wav"
    print("extracting timeline audio…", flush=True)
    build_voice_wav(project, pdir, wav)
    print("transcribing…", flush=True)
    words = transcribe(wav, args.lang, dur, tmpdir)
    groups = group_words(words)

    # Empty result → keep whatever captions exist rather than wiping them.
    if not groups:
        print("done: 0 captions (no speech detected; kept existing captions)", flush=True)
        return

    # Anchor the new captions with the SAME project that built the WAV (`project`
    # above), NOT a fresh disk read — a clip edit auto-saved during the (slow)
    # large-v3 transcription would otherwise misalign every word. Build
    # source-anchored groups (REN-115): map TIMELINE words back to SOURCE seconds.
    new_caps = []
    for g in groups:
        runs = []  # [(src, [(w, s0, s1), ...])]
        for w, t0, t1 in g:
            ci, s0 = out_to_source(project, t0)
            if ci is None:
                continue
            cj, s1 = out_to_source(project, t1)
            clip = project["clips"][ci]
            src = clip.get("src", "main")
            if cj != ci:
                s1 = clip["out"]
            item = (w, s0, max(s1, s0 + 0.05))
            if runs and runs[-1][0] == src:
                runs[-1][1].append(item)
            else:
                runs.append((src, [item]))
        for src, ws in runs:
            if not ws:
                continue
            new_caps.append({
                "id": "g" + uuid.uuid4().hex[:7],
                **CAPTION_STYLE, "capAnchor": "source", "src": src,
                "words": [{"w": w, "t0": round(s0, 2), "t1": round(s1, 2)} for w, s0, s1 in ws],
            })

    # Write into a FRESH read (the app may have auto-saved clip edits while we
    # transcribed) and REPLACE captions — but keep any the user hand-edited
    # (marked "edited"); only auto-generated ones are refreshed.
    disk = load_project(pdir / "project.json")
    kept = [c for c in disk.get("captions", []) if c.get("edited")]
    disk["captions"] = kept + new_caps
    added = len(new_caps)
    (pdir / "project.json").write_text(json.dumps(disk, ensure_ascii=False, indent=1))
    print(f"PROGRESS {int(dur)}/{int(dur)}", flush=True)
    print(f"done: {added} captions", flush=True)


if __name__ == "__main__":
    main()
