"""Per-source transcript for the Descript-style editor (REN-83): transcribe ONE
source file's full audio into SOURCE-time words and write a small JSON so the
transcript panel can map words <-> clips.

Usage: python transcribe_source.py <project_dir> <media_rel> <out_rel> [--lang auto]
Writes <project_dir>/<out_rel> = {"lang": "...", "words": [{"w","t0","t1"}]}
with t0/t1 in SECONDS of the source. Prints "PROGRESS i/n" and "done: <out_rel>".
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from renderer import FFMPEG, resolve
from transcribe import transcribe  # reuse whisper-cli word timestamps

FFPROBE = str(Path(FFMPEG).parent / "ffprobe") if "/" in FFMPEG else "ffprobe"


def probe_dur(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("media")
    ap.add_argument("out")
    ap.add_argument("--lang", default="auto")
    args = ap.parse_args()

    pdir = Path(args.project_dir)
    src = resolve(pdir, args.media)
    if not src or not Path(src).is_file():
        sys.exit(f"source not found: {args.media}")
    dur = probe_dur(src) or 1.0

    tmp = tempfile.mkdtemp(prefix="vstudio_ts_")
    wav = Path(tmp) / "a.wav"
    print("extracting audio…", flush=True)
    r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"audio extraction failed: {r.stderr[-400:]}")

    print("transcribing…", flush=True)
    words = transcribe(wav, args.lang, dur, tmp)  # [(word, t0, t1)] in source seconds

    out = pdir / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "lang": args.lang,
        # freshness signature: lets the server detect a transcript that belongs
        # to an older/replaced source file and refuse to use it (REN-127)
        "src_size": Path(src).stat().st_size,
        "words": [{"w": w, "t0": round(t0, 3), "t1": round(t1, 3)} for w, t0, t1 in words],
    }, ensure_ascii=False))
    # Register the transcript ON THE SOURCE, here, where it cannot be forgotten.
    # Writing only the file left project.json pointing at nothing whenever the
    # caller did not remember to update it — which the AI never does, since the
    # prompt tells it to run this script and then read the JSON directly. The
    # result was a project whose transcript existed on disk while every consumer
    # (take detector, captions, the edit gate) reported "no transcript, transcribe
    # first" and refused to edit.
    try:
        pj = pdir / "project.json"
        proj = json.loads(pj.read_text())
        want = str(Path(src).resolve())
        for key, s in (proj.get("sources") or {}).items():
            p = s.get("path") or ""
            have = Path(p) if p.startswith("/") else (pdir / p)
            if str(have.resolve()) == want:
                if s.get("transcript") != args.out:
                    s["transcript"] = args.out
                    pj.write_text(json.dumps(proj, ensure_ascii=False, indent=1))
                    print(f"registered transcript on source '{key}'", flush=True)
                break
    except (OSError, ValueError) as e:  # never fail the transcription over this
        print(f"could not register the transcript on the project ({e})", flush=True)

    print(f"PROGRESS {int(dur)}/{int(dur)}", flush=True)
    print(f"done: {args.out}", flush=True)


if __name__ == "__main__":
    main()
