"""Where to cut between two words, measured on the audio (REN-164).

The Cut tab used to place a boundary at the midpoint of `prev.t1 … next.t0`,
straight from whisper's word stamps. Those are not physical boundaries. This
project's own transcript, measured:

    685 of 916 consecutive word pairs have a gap of EXACTLY zero
    159 of 916 actually overlap (down to -0.1 s)

With a zero gap the midpoint IS `prev.t1`, so there is no margin at all; with an
overlap it lands INSIDE the previous word. That is what ate "dia" when he asked
to delete "ganhar muito".

The envelope is the only thing here that does not lie, so the boundary comes
from it: find the real dip between the two words and cut in the middle of it.
When there is no dip — the words genuinely run together — this says so instead
of pretending, and the caller warns rather than quietly cutting into speech.

Deliberately NOT the approach tried in `wave-1-wip` (a fixed GUARD/LEAD_SHIFT/
MIN_CUT budget on top of the same stamps): measured on 30 real cases that traded
"eats the previous word" for "leaves the head of the word you deleted audible",
which is worse.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import takes as T


# how far past the nominal boundary to look for the dip. Whisper's clock drifts
# against the audio by a few hundred ms on this material; a window smaller than
# the drift finds nothing and falls back for no reason. Capped by the two words'
# own extents below so it can never wander into a third word.
DRIFT = 0.35
# a dip shorter than this is a stop consonant, not a pause — same figure the
# take splitter uses, so both agree on what counts as silence.
MIN_PAUSE = T.SIL_MIN
# A cut this close to the level of the speech around it has nowhere quiet to
# land — it goes through a vowel and the join is heard. Further below, it sits
# in the natural dip between syllables and is inaudible. Calibrated on 2356 real
# word pairs: 4% of ordinary edits cross this line.
SPLICE_MARGIN_DB = 6.0


# ---------------------------------------------------------------- audio cache

_ENV: dict[tuple, tuple] = {}   # (path, mtime, size) -> (env, thr, floor, runs)


def _audio(pdir: Path, media: Path):
    """(envelope, threshold_db, noise_floor, speech_runs) for a source, cached
    in-process.

    Decoding an 8-minute 4K source takes seconds; he cuts a dozen times in a
    session and every one of them asks the same question of the same audio.
    """
    try:
        st = media.stat()
        key = (str(media), int(st.st_mtime), st.st_size)
    except OSError:
        return None, None, None, None
    if key in _ENV:
        return _ENV[key]
    tmp = pdir / "media" / f"_cut_{os.getpid()}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    samples, sr = T._load_audio(media, tmp)
    try:
        tmp.unlink()
    except OSError:
        pass
    env = T.envelope(samples, sr)
    if env is None:
        _ENV[key] = (None, None, None, None)
        return _ENV[key]
    thr, floor = T.threshold_db(env)
    runs = T.speech_runs(env, thr, len(env) * T.HOP)
    _ENV[key] = (env, thr, floor, runs)
    return _ENV[key]


def _run_of(runs, t0, t1):
    """The stretch of speech a word actually lives in — most overlap with its
    stamped span. This is the only question whisper's stamps are still good
    enough to answer: they drift, but not by a whole speech run."""
    best, ov = None, 0.0
    for a, b in runs or ():
        o = min(t1, b) - max(t0, a)
        if o > ov:
            best, ov = (a, b), o
    return best


# ------------------------------------------------------------------ the rule

def _dips(env, thr, lo, hi):
    """[(t0, t1)] of every stretch below the speech threshold inside the
    window, in envelope resolution."""
    a = max(0, int(lo / T.HOP))
    b = min(len(env), int(hi / T.HOP) + 1)
    out, start = [], None
    for i in range(a, b):
        if env[i] < thr:
            if start is None:
                start = i
        elif start is not None:
            out.append((start * T.HOP, i * T.HOP))
            start = None
    if start is not None:
        out.append((start * T.HOP, b * T.HOP))
    return out


def _quietest(env, lo, hi, win=3):
    """(centre, level_db) of the quietest `win` hops in the window — the least
    bad place to cut when the two words really do touch."""
    a = max(0, int(lo / T.HOP))
    b = min(len(env), int(hi / T.HOP) + 1)
    if b - a < win + 1:
        return None, None
    best, bi = None, a
    for i in range(a, b - win):
        v = float(sum(env[i:i + win])) / win
        if best is None or v < best:
            best, bi = v, i
    return (bi + win / 2) * T.HOP, best


def boundary(env, thr, a_end, b_start, a_start=None, b_end=None, margin=0.03,
             runs=None, floor=None):
    """Cut time between the word ending at `a_end` and the one starting at
    `b_start`. Either side may be None (cutting at the very edge of the take).

    Returns {"t", "measured", "gap"}:
      measured=True  the two words are separated by real silence and the cut
                     sits inside it — the neighbour cannot be clipped
      measured=False they are one continuous sound; `t` is the quietest instant
                     available and the caller says it will sound spliced.
    """
    if a_end is None and b_start is None:
        return {"t": 0.0, "measured": False, "gap": 0.0}
    if env is None:                     # no audio decode — nothing to measure
        want = (b_start - margin if a_end is None
                else a_end + margin if b_start is None
                else (a_end + b_start) / 2)
        return {"t": round(want, 3), "measured": False, "gap": 0.0}

    # ---- 1. the speech runs decide, not the stamps ----------------------
    #
    # This is the whole point of the issue. Whisper's word stamps are not
    # physical boundaries and on this material they are not even approximately
    # right: "vive" is stamped 10.89 -> 16.32, a 5.4-second word, while its
    # voice actually stops at 11.57. A window centred on 16.32 sits in the
    # middle of the NEXT sentence and finds no pause, so a stamp-driven rule
    # either refuses or cuts through speech. Asking which stretch of speech
    # each word lives in is a question the stamps can still answer — they drift,
    # but not by a whole run — and the silence between those two stretches is
    # the boundary, measured, whatever the stamps claim.
    ra = _run_of(runs, a_start if a_start is not None else a_end, a_end) if a_end is not None else None
    rb = _run_of(runs, b_start, b_end if b_end is not None else b_start) if b_start is not None else None

    if ra and rb and ra != rb and rb[0] > ra[1]:
        return {"t": round((ra[1] + rb[0]) / 2, 3), "measured": True,
                "gap": round(rb[0] - ra[1], 3)}
    if ra is None and rb:               # cutting in before the first word
        return {"t": round(max(0.0, rb[0] - margin), 3), "measured": True,
                "gap": round(margin, 3)}
    if rb is None and ra:               # cutting out after the last word
        return {"t": round(ra[1] + margin, 3), "measured": True,
                "gap": round(margin, 3)}

    # ---- 2. one continuous sound: find the least bad instant ------------
    #
    # Same run (or no runs at all): the two words genuinely touch. Search
    # between the stamps, fenced by the two words' own extents so it cannot
    # wander into a third word, and take the quietest point.
    if a_end is None:
        want, lo, hi = b_start - margin, max(0.0, b_start - DRIFT), b_start + min(DRIFT, 0.1)
    elif b_start is None:
        want, lo, hi = a_end + margin, a_end - min(DRIFT, 0.1), a_end + DRIFT
    else:
        want = (a_end + b_start) / 2
        lo, hi = min(a_end, b_start) - DRIFT, max(a_end, b_start) + DRIFT
    if ra:                              # never leave the run they share
        lo, hi = max(lo, ra[0]), min(hi, ra[1])
    if a_start is not None:
        lo = max(lo, a_start)
    if b_end is not None:
        hi = min(hi, b_end)
    lo = max(0.0, lo)
    if hi <= lo:
        return {"t": round(want, 3), "measured": False, "gap": 0.0}

    dips = _dips(env, thr, lo, hi)
    real = [d for d in dips if d[1] - d[0] >= MIN_PAUSE]
    if real:
        # a pause inside a run happens when the run-splitter's SIL_MIN kept them
        # together. Nearest the nominal boundary wins — the same bias
        # takes.py's _split_time settled on.
        real.sort(key=lambda d: abs((d[0] + d[1]) / 2 - want))
        d = real[0]
        return {"t": round((d[0] + d[1]) / 2, 3), "measured": True,
                "gap": round(d[1] - d[0], 3)}

    q, level = _quietest(env, lo, hi)
    widest = max((d[1] - d[0] for d in dips), default=0.0)
    # `level` is the chosen instant measured against the SURROUNDING SPEECH, not
    # against the noise floor: -15 dB means the cut sits in the natural dip
    # between two syllables and no one will hear it; 0 dB means there is nowhere
    # quiet to cut and the blade goes through a vowel.
    #
    # Measured over 2356 real touching pairs: the median cut lands 15 dB below
    # the surrounding speech and only 4% land within 6 dB of it. Against the
    # noise floor instead, 81% of ordinary edits looked alarming — a warning
    # that fires on four cuts out of five is one he learns to ignore.
    rel = None
    if level is not None:
        import numpy as np  # noqa: PLC0415
        seg = env[int(ra[0] / T.HOP):int(ra[1] / T.HOP)] if ra else env[int(lo / T.HOP):int(hi / T.HOP)]
        if getattr(seg, "size", 0) >= 5:
            rel = level - float(np.percentile(seg, 75))
    return {"t": round(q if q is not None else want, 3),
            "measured": bool(rel is not None and rel < -SPLICE_MARGIN_DB),
            "level": None if rel is None else round(rel, 1),
            "gap": round(widest, 3)}


# ------------------------------------------------------------------- entry

def resolve(pdir: Path, src_key: str, gaps: list[dict]) -> list[dict]:
    """Measure a batch of boundaries for one source. `gaps` items carry
    prev/next (word t1 / t0, either may be null), optional prevStart/nextEnd
    fences, and a margin."""
    project = json.loads((pdir / "project.json").read_text())
    src = (project.get("sources") or {}).get(src_key) or {}
    media = T.resolve(pdir, src.get("path"))
    env, thr, floor, runs = _audio(pdir, Path(media)) if media else (None, None, None, None)
    out = []
    for g in gaps:
        out.append(boundary(
            env, thr,
            g.get("prev"), g.get("next"),
            a_start=g.get("prevStart"), b_end=g.get("nextEnd"),
            margin=float(g.get("margin") or 0.03),
            runs=runs, floor=floor,
        ))
    return out


def warm(pdir: Path, src_key: str) -> bool:
    """Decode and cache the envelope ahead of the first cut. Called when the Cut
    tab opens so the first delete is not the one that pays for the decode."""
    try:
        project = json.loads((pdir / "project.json").read_text())
        src = (project.get("sources") or {}).get(src_key) or {}
        media = T.resolve(pdir, src.get("path"))
        if not media:
            return False
        return _audio(pdir, Path(media))[0] is not None
    except Exception:  # noqa: BLE001 — warming is best-effort
        return False
