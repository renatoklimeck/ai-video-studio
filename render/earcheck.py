"""EARS for the assembler — re-listen at every clip head before captions run.

Why this exists: whisper, given audio containing an aborted attempt AND its
retake, fuses them into one clean sentence. Measured on this machine it does so
in the full pass, inside a 4-second slice, and — decisive test — ACROSS A FULL
SECOND OF INSERTED SILENCE between two stitched slices: it stretched one word
2.7s over the gap to keep the sentence fluent, and the retake's own opening was
never emitted. The decoder is a language model; fluency wins whenever both
attempts are inside one decode window.

The only watertight isolation is one whisper INVOCATION per slice: what is not
in the file cannot be fused in. The small model makes that affordable (~3-5s
per micro-slice, measured, and it heard every restart the big models smoothed
over). So, per suspicious clip head (more than one speech run in its first
seconds):

  A = the first run alone      → does it OPEN the script line?
  B = from just inside A's end → does the line START AGAIN here?

Both yes → A is an aborted attempt: clip.in moves to the retake's speech-run
start, and the transcript words for the moved head are re-stamped from B so the
captions stay in sync. A single run, or a B that merely continues the sentence
("E sim, | as pessoas acham…"), leaves the clip alone — that is a real pause.

Fail-open by design: any error leaves the clips exactly as assembled.

Usage: python earcheck.py <project_dir>   (standalone, audits current clips)
"""
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from renderer import FFMPEG, resolve  # noqa: E402
from takes import _load_audio, _norm, envelope, speech_runs, threshold_db  # noqa: E402
from transcribe import transcribe  # noqa: E402

HEAD_SPAN = 4.2     # how far past clip.in runs are considered part of the head
RUN_HEAD = 2.2      # seconds transcribed from the top of each run
GAP = 1.0           # silence between stitched micro-slices
MIN_SHIFT = 0.15    # smaller corrections are noise
MAX_SHIFT = 3.4     # never move a head further than this
MATCH_N = 2         # opening words of the script line that must match


def _small_model():
    """Cheapest honest ear: per-slice invocations make model load the dominant
    cost, and the small model loads in ~1s while hearing restarts the large
    ones smooth away."""
    for cand in (Path.home() / "loom-tools" / "models" / "ggml-small.bin",
                 Path.home() / ".models" / "ggml-small.bin"):
        if cand.exists():
            return cand
    return None


def _listen(media, start, dur, tmpdir, tag):
    """Transcribe ONE slice in its own whisper invocation; source-time words."""
    seg = Path(tmpdir) / f"{tag}.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
                    "-t", f"{dur:.3f}", "-i", str(media),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(seg)],
                   check=True, timeout=120)
    words = transcribe(seg, "pt", dur, tmpdir, use_vad=False, model=_small_model())
    toks, timed = [], []
    for w, t0, t1 in words:
        tk = _norm(w)
        if tk:
            toks.append(tk)
            timed.append((w, t0 + start, t1 + start))
    return toks, timed


def _sim(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def _find_open(tokens, line, window=4):
    """Index where the line's opening bigram appears among the first `window`
    tokens, or None. FUZZY on purpose: he says "venda" where the approved
    script says "vende", and an exact match silently skipped the whole check —
    which is how an aborted take survived at a clip head."""
    want = _norm(line or "").split()[:MATCH_N]
    if len(want) < MATCH_N:
        return None
    for i in range(0, min(window, max(0, len(tokens) - MATCH_N + 1))):
        if all(_sim(tokens[i + k], want[k]) >= 0.72 for k in range(MATCH_N)):
            return i
    return None


def _opens_line(tokens, line, window=4):
    return _find_open(tokens, line, window) is not None


def audit_heads(pdir: Path, clips, lines_for_clip, lang="auto"):
    """Adjust clip heads in place; returns (report, transcript_patches)."""
    import os
    dbg = os.environ.get("EAR_DEBUG") == "1"
    project = json.loads((pdir / "project.json").read_text())
    src_key = clips[0].get("src", "main") if clips else "main"
    src = (project.get("sources") or {}).get(src_key) or {}
    media = resolve(pdir, src.get("path"))
    if not media or not Path(media).is_file():
        return ["ear skipped: source media not found"], []

    report, patches = [], []
    with tempfile.TemporaryDirectory(prefix="vstudio_ear_") as tmp:
        wav16 = Path(tmp) / "full.wav"
        samples, sr = _load_audio(media, wav16)
        env = envelope(samples, sr)
        if env is None:
            return ["ear skipped: could not read audio"], []
        thr, _ = threshold_db(env)
        runs = speech_runs(env, thr, len(env) * 0.01 + 1)

        def commit(i, c, deciding_timed, open_idx, old_in, why,
                   floor=None, slice_start=None, direction="fwd"):
            """Move the head to where the line opens. The matched word's stamp
            is the base, but a stamp at a slice EDGE lies in both directions —
            so a floor (end of the junk run) clamps it up, and a token-0 match
            is clamped down to the slice start (whisper heard the line from the
            very beginning of the slice, so it starts at or before it)."""
            w0 = deciding_timed[open_idx]
            new_in = w0[1] - 0.04
            if open_idx == 0 and slice_start is not None:
                new_in = min(new_in, slice_start + 0.02)
            if floor is not None:
                new_in = max(new_in, floor)
            new_in = round(max(0.0, new_in), 3)
            # DIRECTION is part of the meaning. Pass 1 removes junk BEFORE the
            # line (may only move forward); pass 2 recovers words the head cut
            # off (may only move backward). Without this, a mis-assigned line in
            # standalone mode walked a CORRECT head forward onto a phrase found
            # deeper in the clip and ate its true opening.
            delta = new_in - old_in
            ok = (MIN_SHIFT < delta <= MAX_SHIFT) if direction == "fwd" \
                else (MIN_SHIFT < -delta <= 1.5)
            if not ok:
                return False
            c["in"] = new_in
            report.append(f"  ear: clip {i + 1} head {old_in:.2f} → {new_in:.2f} ({why})")
            line = lines_for_clip[i]
            bt = [(w, t0, t1) for w, t0, t1 in deciding_timed[open_idx:] if t1 > new_in]
            line_words = (line or "").split()
            a = [_norm(w) for w, _x, _y in bt]
            b = [_norm(w) for w in line_words]
            fresh, prev = [], new_in - 0.01
            for blk in difflib.SequenceMatcher(None, a, b).get_matching_blocks():
                for k in range(blk.size):
                    _w, t0, t1 = bt[blk.a + k]
                    t0 = max(prev + 0.01, new_in + 0.01 if not fresh else t0)
                    fresh.append({"w": line_words[blk.b + k], "t0": round(t0, 3),
                                  "t1": round(max(t0 + 0.04, t1), 3)})
                    prev = t0
            if fresh:
                patches.append((max(0.0, min(old_in, new_in) - 0.25),
                                fresh[-1]["t1"] + 0.02, fresh))
            return True

        for i, c in enumerate(clips):
            line = lines_for_clip[i]
            moved = False

            # PASS 1 — junk before the line: more than one speech run in the
            # head. If the audio AFTER the first run opens the line cleanly,
            # everything before that opening is not the line — whatever the
            # first run was (a clear retry, or a mumble the ear can't even
            # parse: aborts are usually mumbled, so demanding the abort ALSO
            # read back as the line was wrong and let one through).
            head = [r for r in runs if r[1] > c["in"] + 0.05 and r[0] < c["in"] + HEAD_SPAN]
            if len(head) >= 2 and (head[0][1] - c["in"]) <= MAX_SHIFT:
                rb = head[0][1]
                btoks, btimed = _listen(media, max(0.0, rb - 0.25), 3.0, tmp, f"b{i}")
                if dbg:
                    print(f"ear-dbg clip {i+1} B={' '.join(btoks[:7])!r}")
                j = _find_open(btoks, line)
                if j is not None and btimed:
                    moved = commit(i, c, btimed, j, c["in"],
                                   "heard the line start again after the first run",
                                   floor=rb + 0.02)

            # PASS 2 — the opposite failure: the head CUT INTO the line (a
            # split placed inside continuous speech ate its first words).
            # Listen at the head: if the line does not open here but does open
            # a little earlier, the earlier position is the truth. Nearest
            # candidate first, so a still-earlier aborted attempt of the same
            # line can never win over the kept take's own start.
            if not moved:
                vtoks, vtimed = _listen(media, max(0.0, c["in"] - 0.04), 2.4, tmp, f"v{i}")
                if dbg:
                    print(f"ear-dbg clip {i+1} V={' '.join(vtoks[:7])!r}")
                if _find_open(vtoks, line) is None and vtoks:
                    run0 = next((r for r in runs if r[0] <= c["in"] <= r[1] + 0.02), None)
                    cands = [c["in"] - 0.35, c["in"] - 0.7, c["in"] - 1.1]
                    if run0 and run0[0] < c["in"] - 0.05:
                        cands.append(run0[0] - 0.04)
                    seen_c = set()
                    for cand in sorted((x for x in cands if x >= c["in"] - 1.5 and x >= 0),
                                       reverse=True):
                        key = round(cand, 1)
                        if key in seen_c:
                            continue
                        seen_c.add(key)
                        wtoks, wtimed = _listen(media, cand, 2.6, tmp, f"w{i}_{key}")
                        if dbg:
                            print(f"ear-dbg clip {i+1} W@{cand:.2f}={' '.join(wtoks[:7])!r}")
                        j = _find_open(wtoks, line)
                        if j is not None and wtimed:
                            if commit(i, c, wtimed, j, c["in"],
                                      "the head had cut into the line — walked back to its first word",
                                      floor=(run0[0] - 0.06) if run0 else None,
                                      slice_start=cand, direction="back"):
                                break

    return report, patches


def apply_patches(pdir: Path, src_key, patches):
    """Replace the transcript's words inside each moved-head window with the
    slice-accurate ones. Keeps a one-time backup of the pristine transcript."""
    project = json.loads((pdir / "project.json").read_text())
    src = (project.get("sources") or {}).get(src_key) or {}
    tp = src.get("transcript") or f"media/transcript_{src_key}.json"
    path = pdir / tp
    if not path.exists():
        return
    bak = path.with_suffix(".orig.json")
    if not bak.exists():
        bak.write_bytes(path.read_bytes())
    data = json.loads(path.read_text())
    words = data.get("words") or []
    for lo, hi, fresh in patches:
        words = [w for w in words if not (lo - 1e-3 <= w["t0"] < hi)]
        words += fresh
    words.sort(key=lambda w: w["t0"])
    data["words"] = words
    path.write_text(json.dumps(data, ensure_ascii=False))


def main():
    pdir = Path(sys.argv[1])
    project = json.loads((pdir / "project.json").read_text())
    clips = project.get("clips") or []
    lines = [l for l in (project.get("script") or "").splitlines() if l.strip()]
    # standalone has no pick table: find each clip's line by matching the
    # transcript text inside the clip (positional guessing was off by several
    # lines whenever a line spans two clips or vice versa)
    src0 = clips[0].get("src", "main") if clips else "main"
    tp = (project.get("sources") or {}).get(src0, {}).get("transcript") \
        or f"media/transcript_{src0}.json"
    try:
        tx = json.loads((pdir / tp).read_text()).get("words") or []
    except (OSError, ValueError):
        tx = []
    lf = []
    for c in clips:
        ws = " ".join(w["w"] for w in tx
                      if min(w["t1"], c["out"]) - max(w["t0"], c["in"]) > 0)
        best = max(lines, default="",
                   key=lambda l: difflib.SequenceMatcher(None, _norm(ws), _norm(l)).ratio())
        lf.append(best)
    rep, patches = audit_heads(pdir, clips, lf)
    print("\n".join(rep) or "ear: no head needed moving")
    if patches:
        apply_patches(pdir, clips[0].get("src", "main") if clips else "main", patches)
    if rep:
        project["clips"] = clips
        (pdir / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
