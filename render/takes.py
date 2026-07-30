"""Deterministic TAKE DETECTOR — the primitive the AI edit is built on.

The old flow let the model write clip in/out numbers by reading a transcript.
A model can't hear and is poor at precise arithmetic over dozens of cuts, so
boundaries landed mid-word and quiet REHEARSAL takes were picked over the real
ones. This module removes that entire class of failure:

  * segments the source into TAKES from the audio's own energy envelope, so
    every boundary sits in measured silence — never mid-word;
  * decides SPLIT vs MERGE from the TEXT, not from how long the pause was
    (REN-135/136). A pause where the speaker RESTARTS the line is a false start
    and must be cut; a pause where he CONTINUES is intentional delivery and must
    be kept, however long it is;
  * throws out whisper hallucinations before they become takes or captions;
  * measures DELIVERY per take (loudness vs the session, framing, whether it
    ends on a complete sentence), which is how a rehearsal becomes detectable.

The AI then only picks take NUMBERS. All timing is computed here.

Usage:
  python takes.py <project_dir> [--src main] [--json out.json]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from renderer import FFMPEG, resolve  # noqa: E402

# --- silence -----------------------------------------------------------------
# Cuts are placed on REAL audio energy, never on transcript word gaps: whisper
# reports 0.00s (even negative) between back-to-back sentences, so every pause
# in this video is invisible in the transcript and visible in the waveform.
#
# The threshold is adaptive because every mic is different (students run this on
# their own recordings). Anchoring it to the NOISE FLOOR alone was too generous:
# this room floors at -76 dB, so floor+20 = -56 dB counted breathing and chair
# creak as speech and left up to 0.9s of dead air on a take. Anchoring it to the
# SPEECH level instead tracks the voice, and the floor term keeps it sane in a
# noisy room. Measured on a real 6-minute take: median dead air +0.05s.
HOP = 0.01               # envelope resolution (10ms — the pads below can only
                         # be as tight as this is precise)
SPEECH_REL_DB = 28.0     # below the 85th-percentile (speech) level
FLOOR_MARGIN_DB = 14.0   # ...but never below the noise floor + this
SIL_DB_RANGE = (-58.0, -26.0)
SIL_MIN = 0.12           # a dip shorter than this is a stop consonant, not a pause

# --- how much room to leave around the speech --------------------------------
# The run edges are already the real start/end of the voice, so these only exist
# so the cut does not land on the attack of the first consonant. Renato: "ele tem
# que começar e acabar imediatamente junto com a fala."
# One envelope step of slack, so the cut cannot land on the attack of the first
# consonant or clip the decay of the last — and nothing more. The export already
# puts an 8ms declick fade on every cut, so this does not need to hide a click.
LEAD_PAD = 0.02
TAIL_PAD = 0.03

# --- split vs merge ----------------------------------------------------------
# A pause longer than this is a break between separate deliveries no matter what
# the text says (he stopped, thought, restarted).
HARD_GAP = 0.85
# How many opening words must coincide for the next run to count as a RESTART of
# the unfinished clause before it. 2 is enough because the clause also has to be
# almost entirely consumed by the match — that is what stops the rhetorical
# anaphora in this script ("Vê sua ambição… / Vê seu padrão…") from reading as a
# false start: those clauses are complete and long, so they never qualify.
RESTART_MIN_WORDS = 2
RESTART_HEAD = 8
# A brief burst, a pause, then a much longer delivery = he started over. This is
# the shape the TEXT cannot see: whisper often transcribes the aborted burst
# badly or not at all ("Você tem" came back as just "tem"), so there is no
# repetition to detect — only the audio shows it. Kept narrow on purpose: a
# rhetorical short opener followed by a beat must survive.
FALSE_LEAD_MAX = 1.0     # the burst is at most this long
FALSE_LEAD_GAP = 0.30    # ...followed by at least this much silence
ELLIPSIS_MAX_WORDS = 8   # an abandoned clause longer than this is a pause for effect
FALSE_LEAD_RATIO = 3.0   # ...and the delivery after it is at least this much longer

# --- delivery ----------------------------------------------------------------
QUIET_DB = 4.0           # this much under the session median = likely rehearsal
# framing deviation (vs this video's own median) above which the speaker is out
# of position — measured 8x the normal deviation on a real rehearsal take
FRAMING_BAD = 0.20

# --- transcript sanity -------------------------------------------------------
# Whisper sometimes emits a loop: 18 words stacked inside 0.18s ("Vê sua
# autoconfiança e chama de…" three times over) on audio that holds one short
# phrase. Those words are not spoken, and left in they become take text, take
# boundaries AND captions. Portuguese tops out around 9 words/s, so anything
# past this is physically impossible and gets dropped.
MAX_WPS = 20.0
RATE_WIN = 4             # words per window used to measure the rate
LOOP_WINDOW = 8.0        # how far around a burst to look for the phrase it loops
RETIME_MAX_STEP = 0.25   # seconds per word when re-spreading a collapsed burst


def _norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def trails_off(text):
    """Ends in an ellipsis. Whisper writes '…' exactly where the speaker gave up
    mid-phrase, which makes it the cheapest false-start signal available."""
    return bool(re.search(r"(\.\.\.|…)\s*$", (text or "").strip()))


def ends_sentence(text):
    t = (text or "").strip()
    return bool(re.search(r"[.!?]\s*$", t)) and not trails_off(t)


def is_loop(blk_words, near_text):
    """Is this impossible-rate burst text that was NEVER SPOKEN?

    Getting this wrong is expensive in both directions: call a real delivery a
    loop and its words are deleted from the take table AND the captions; call a
    loop real and fabricated words are spread over the timeline.

    Two independent tests, because whisper's loops come in two shapes:
      (a) INTERNALLY repetitive — the burst says one short phrase over and over
          ("Obrigado por assistir" seven times). Nothing outside the burst
          matches it, so comparing against neighbouring text can never catch it.
      (b) the WHOLE burst repeats text that really is spoken nearby.

    Matching only the burst's first few words (the earlier rule) fails badly: a
    retake opens with the same words as the attempt it replaces — that is what a
    retake IS — so a good take with a collapsed clock got deleted whole.
    """
    return (self_repeats(blk_words)
            or " ".join(t for t in (_norm(w["w"]) for w in blk_words) if t) in near_text)


def self_repeats(blk_words):
    """The burst says one short phrase over and over ("Obrigado por assistir"
    seven times). Nothing outside the burst matches it, so no comparison against
    neighbouring text can catch this shape."""
    toks = [t for t in (_norm(w["w"]) for w in blk_words) if t]
    if len(toks) < RATE_WIN:
        return False
    for n in range(2, 9):
        if len(toks) < n * 2:
            break
        first = toks[:n]
        hits = sum(1 for i in range(0, len(toks) - n + 1, n) if toks[i:i + n] == first)
        if hits >= 2 and hits * n >= len(toks) * 0.6:
            return True
    return False


def sanitize(words, runs=None):
    """Fix the two ways whisper's word clock lies. Returns (kept, dropped).

    Both look identical at first — a burst of words at an impossible rate — but
    they need opposite treatment, and getting that wrong costs real caption text:

      LOOP: the burst REPEATS a phrase that is also spoken nearby (57 words in
      0.14s here, "Fica quase impossível voltar a viver com menos" over and
      over). Nobody said those words; they are dropped.

      COLLAPSED: the burst is text he really did say, stamped on top of itself
      ("E eu acho que" in 0.17s). Dropping it would silently delete words from a
      caption, so it is kept and spread evenly over the room around it.
    """
    words = list(words)
    bad = set()
    _ = runs  # (used below once the impossible-rate blocks are known)
    for i in range(len(words)):
        j = min(len(words), i + RATE_WIN)
        if j - i < RATE_WIN:
            break
        span = words[j - 1]["t1"] - words[i]["t0"]
        if span <= 0 or (j - i) / span > MAX_WPS:
            bad.update(range(i, j))
    if not bad:
        return words, []

    blocks, cur = [], []
    for i in sorted(bad):
        if cur and i == cur[-1] + 1:
            cur.append(i)
        else:
            if cur:
                blocks.append(cur)
            cur = [i]
    blocks.append(cur)

    # Which stretches of real speech already have a surviving word on them. A
    # run with none is audio nobody has accounted for — that is where a burst of
    # REAL words with a collapsed clock belongs.
    covered = set()
    if runs:
        for k, w in enumerate(words):
            if k in bad:
                continue
            mid = (w["t0"] + w["t1"]) / 2
            for ri, (a, b) in enumerate(runs):
                if a - 0.25 <= mid <= b + 0.25:
                    covered.add(ri)
                    break

    dropped = set()
    for blk in blocks:
        lo, hi = blk[0], blk[-1]
        blk_words = [words[k] for k in blk]
        if self_repeats(blk_words):
            dropped.update(blk)               # a loop is a loop, wherever it sits
            continue
        # Does the recording hold speech that no surviving word explains, near
        # where this burst was stamped? If so the burst is that speech — a real
        # retake whose clock collapsed — and it gets placed ON that audio.
        # Comparing against neighbouring TEXT cannot decide this: a retake
        # repeats the line it replaces, so an echo test deleted good takes
        # whenever the two attempts were within a few seconds of each other,
        # which is exactly how someone re-records a line.
        home = None
        if runs:
            t = words[lo]["t0"]
            free = [ri for ri, (a, b) in enumerate(runs)
                    if ri not in covered and b - a >= 0.25
                    and abs((a + b) / 2 - t) <= LOOP_WINDOW]
            if free:
                home = min(free, key=lambda ri: abs(sum(runs[ri]) / 2 - t))
        if home is not None:
            a, b = runs[home]
            step = (b - a) / len(blk)
            for n, k in enumerate(blk):
                words[k] = dict(words[k], t0=round(a + n * step, 3),
                                t1=round(a + (n + 1) * step, 3), retimed=True)
            covered.add(home)
            continue
        near = " ".join(_norm(w["w"]) for k, w in enumerate(words)
                        if k not in bad
                        and abs(w["t0"] - words[lo]["t0"]) <= LOOP_WINDOW)
        if " ".join(t for t in (_norm(w["w"]) for w in blk_words) if t) in near:
            dropped.update(blk)               # echoes real speech AND has no audio of its own
            continue
        # Room is measured between the REAL words around the burst. When there is
        # no real word on one side (the burst opens or closes the transcript),
        # fall back to giving it a natural speaking span instead of to its own
        # broken timestamps — those make room ~0 by construction, so a burst at
        # either end of the transcript was always dropped and real words with it.
        span = len(blk) * RETIME_MAX_STEP
        before = next((words[k]["t1"] for k in range(lo - 1, -1, -1) if k not in bad), None)
        after = next((words[k]["t0"] for k in range(hi + 1, len(words)) if k not in bad), None)
        prev_t = before if before is not None else max(0.0, (after or span) - span)
        next_t = after if after is not None else prev_t + span
        room = next_t - prev_t
        if room / max(1, len(blk)) < 0.06:    # no room to place them honestly
            dropped.update(blk)
            continue
        # Spread at a natural rate, ending where the next real word begins —
        # whisper collapses a burst onto the instant just before the speech that
        # follows it. Filling the whole gap instead would leave single words
        # hanging on screen for a second each.
        step = min(room / len(blk), RETIME_MAX_STEP)
        start = max(prev_t, next_t - step * len(blk))
        for n, k in enumerate(blk):
            words[k] = dict(words[k], t0=round(start + n * step, 3),
                            t1=round(start + (n + 1) * step, 3), retimed=True)
    kept = [w for i, w in enumerate(words) if i not in dropped]
    return clamp_to_speech(kept, runs), [w for i, w in enumerate(words) if i in dropped]


def clamp_to_speech(words, runs):
    """No word may claim time where the recording is silent.

    Whisper stretches a word's end deep into the pause after it — "demais..."
    was stamped 254.77→256.45 on audio whose voice stops at 255.66, 0.79s early.
    That single leaked end put the word inside the NEXT take's clip, so a caption
    showed a word the viewer never hears there, and take text opened on the tail
    of the attempt it was supposed to replace.

    Each word is pinned to the speech run it actually lives in. Cheap, and it
    fixes takes and captions at once because both read this list."""
    if not runs:
        return words
    out = []
    for w in words:
        mid = (w["t0"] + w["t1"]) / 2
        run = next((r for r in runs if r[0] - 0.02 <= mid <= r[1] + 0.02), None)
        if run is None:   # drifted into a pause — pin to the run it overlaps most
            best, ov = None, 0.0
            for r in runs:
                o = min(w["t1"], r[1]) - max(w["t0"], r[0])
                if o > ov:
                    best, ov = r, o
            run = best
        if run is None:
            out.append(w)
            continue
        t0 = min(max(w["t0"], run[0]), run[1] - 0.02)
        t1 = max(min(w["t1"], run[1]), t0 + 0.02)
        out.append(w if (t0, t1) == (w["t0"], w["t1"])
                   else dict(w, t0=round(t0, 3), t1=round(t1, 3)))
    return out


def media_duration(media):
    """Real duration of the media (ffprobe), 0.0 if unknown."""
    try:
        probe = str(Path(FFMPEG).with_name(Path(FFMPEG).name.replace("ffmpeg", "ffprobe")))
        r = subprocess.run([probe, "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(media)],
                           capture_output=True, text=True, timeout=60)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _load_audio(media, tmp_wav):
    """16k mono float samples of the source."""
    try:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(media),
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp_wav)],
                       capture_output=True, timeout=900, check=True)
        w = wave.open(str(tmp_wav))
        raw = w.readframes(w.getnframes())
        w.close()
    except Exception:
        return None, 16000
    try:
        import numpy as np
        return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0, 16000
    except ImportError:
        return None, 16000


def envelope(samples, sr):
    """dB RMS per HOP. This is the only thing that knows where the voice is."""
    if samples is None:
        return None
    import numpy as np
    win = max(1, int(HOP * sr))
    n = len(samples) // win
    if n < 10:
        return None
    r = np.sqrt((samples[:n * win].reshape(n, win) ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(r, 1e-6))


def threshold_db(env):
    if env is None:
        return SIL_DB_RANGE[0], None
    import numpy as np
    floor = float(np.percentile(env, 5))
    speech = float(np.percentile(env, 85))
    thr = max(floor + FLOOR_MARGIN_DB, speech - SPEECH_REL_DB)
    return max(SIL_DB_RANGE[0], min(SIL_DB_RANGE[1], thr)), floor


def speech_runs(env, thr, end_cap):
    """[(t0, t1)] of speech, with edges on the real start/end of the voice.
    A silence shorter than SIL_MIN does not break a run."""
    runs, start, quiet = [], None, 0
    for i, v in enumerate(env):
        if v >= thr:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet * HOP >= SIL_MIN:
                runs.append([start * HOP, (i - quiet + 1) * HOP])
                start, quiet = None, 0
    if start is not None:
        runs.append([start * HOP, len(env) * HOP])
    out = []
    for a, b in runs:
        b = min(b, end_cap)
        if b - a > 0.05:
            out.append([round(a, 3), round(b, 3)])
    return out


def _rms_db(samples, sr, t0, t1):
    if samples is None:
        return None
    import numpy as np
    seg = samples[int(max(0, t0) * sr):int(max(0, t1) * sr)]
    if seg.size < 160:
        return None
    return round(float(20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-6))), 1)


def add_vision(pdir: Path, media, takes):
    """Attach framing/face numbers to each take. Deviation is measured against
    this video's OWN median framing, so any room or lens works: a take where he
    is out of position scores ~8x the normal deviation."""
    try:
        import vision  # noqa: PLC0415
    except Exception:
        return
    proxy = None
    for cand in (pdir / "media" / "proxy_main.mp4", pdir / "media" / "proxy_source.mp4"):
        if cand.exists():
            proxy = cand
            break
    try:
        track = vision.analyse(proxy or media)
    except Exception:
        return
    if not track.get("found"):
        return
    stats = []
    for t in takes:
        ss = [s for s in track["samples"] if t["in"] <= s["t"] <= t["out"]]
        face = [s for s in ss if "facing" in s]
        t["face_pct"] = round(len(face) / len(ss), 2) if ss else None
        if face:
            def med(k):
                v = sorted(s[k] for s in face)
                return v[len(v) // 2]
            stats.append((t, med("x"), med("y"), med("h")))
    if not stats:
        return
    mid = lambda vals: sorted(vals)[len(vals) // 2]  # noqa: E731
    mx, my, mh = (mid([s[1] for s in stats]), mid([s[2] for s in stats]),
                  mid([s[3] for s in stats]))
    for t, x, y, h in stats:
        t["framing"] = round(abs(x - mx) + abs(y - my) + abs(h - mh) * 2, 3)


def _common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def is_restart(clause_words, next_words):
    """True when `next_words` starts the same line `clause_words` was in the
    middle of — i.e. the speaker aborted and went again.

    Requires the match to eat almost the whole clause. "Vê sua ambição e…" then
    "Vê sua ambição e chama de ego" matches on 4 of 4 clause words → restart.
    "Vê sua ambição e chama de ego." then "Vê seu padrão alto…" shares only "Vê"
    out of 7 → not a restart, just the next line of a list.
    """
    a = _norm(" ".join(w["w"] for w in clause_words)).split()
    b = _norm(" ".join(w["w"] for w in next_words[:RESTART_HEAD])).split()
    if not a or not b:
        return False
    k = _common_prefix(a, b)
    if k < RESTART_MIN_WORDS:
        return False
    aborted = trails_off(" ".join(w["w"] for w in clause_words))
    # The abandoned clause has to be CONSUMED by what follows: either entirely
    # (a strict prefix of the retry), or all but its last word when whisper
    # marked it as trailing off. Accepting one mismatched last word without that
    # marker read "não é sorte, não é acaso, não é milagre" — one unbroken
    # breath — as three retries, flagged the first two FALSE START and threw
    # them away. Being wrong here DELETES the line, so the tie goes to keeping it.
    if k < len(a) and not (aborted and k == len(a) - 1):
        return False
    # "Não desista, não desista nunca" is emphasis, not a retry.
    return len(a) >= 3 or aborted


def _split_time(prev_w, next_w, sils, lo, hi, env=None):
    """A cut time between two words, placed in measured silence.

    Whisper's word clock drifts against the audio by up to half a second here
    (it hands back t1 = t0 of the next word even across a 0.4s pause), so this
    does NOT require the silence to sit strictly between the two timestamps — it
    takes the silence nearest the nominal boundary inside [lo, hi]. Falling back
    to the midpoint of two drifted timestamps would cut on a syllable."""
    want = (prev_w["t1"] + next_w["t0"]) / 2
    best, bestd = None, float("inf")
    for a, b in sils:
        if b <= lo or a >= hi:
            continue
        c = (max(a, lo) + min(b, hi)) / 2
        d = abs(c - want)
        if d < bestd:
            best, bestd = c, d
    # ANY silence inside the window beats the midpoint of two drifted stamps.
    # Whisper drifts by seconds on this material.
    if best is not None:
        return best
    # No silence at all — he ran the restart straight into the abandoned word
    # ("…vende algo—E se você vende…"). The midpoint of two drifted stamps
    # landed on the attack of the good take's first word, which is the
    # "beginning cut off" complaint. The envelope still knows where the energy
    # dips between the words: cut at the quietest 30ms in the window.
    if env is not None:
        a = int(max(lo, prev_w["t1"] - 0.15) / HOP)
        b = int(min(hi, next_w["t0"] + 0.20) / HOP)
        if b - a >= 4:
            win = 3
            cands = [(float(sum(env[i:i + win])) / win, i) for i in range(a, b - win)]
            if cands:
                return min(cands)[1] * HOP
    return max(lo, min(hi, want))


def find_restarts(words):
    """Indices where the speaker abandoned the line and started it again.

    Runs over the TEXT, not the audio: whisper's timings drift enough that the
    restart often lands inside the wrong speech run, so detecting it from word
    gaps missed three attempts at the same line in a row. The audio is still
    what places the cut — see _split_time."""
    marks, start = [], 0
    for i in range(1, len(words)):
        prev = words[i - 1]["w"]
        if i > start and is_restart(words[start:i], words[i:]):
            marks.append(i)
            start = i
        elif trails_off(prev) and (i - start) <= ELLIPSIS_MAX_WORDS:
            # An ellipsis is whisper writing down the exact instant he gave up on
            # the phrase, and it is the ONLY signal for the common shape where the
            # retry adds words before repeating: "vende algo…" → "E se você vende
            # algo por R$10 mil". Prefix matching cannot see that, so the stumble
            # stayed inside a take the table then advertised as ok.
            # Cutting here is bounded to a SHORT abandoned clause: a dramatic
            # pause comes after a developed thought, and cutting one of those
            # would delete a line he never abandoned.
            marks.append(i)
            start = i
        elif ends_sentence(prev) or trails_off(prev):
            start = i
    # A trailing abandoned clause: he finished the line, then started the NEXT
    # one and gave up ("…máquina do tráfego e de vendas. Enquanto vender…").
    # The loop above cannot see it because the ellipsis is the last word, so
    # nothing follows it to compare against. Split it off here so the good
    # sentence stays usable and the stub is flagged instead of riding along.
    if words and trails_off(words[-1]["w"]):
        tail = max([k for k in range(len(words)) if ends_sentence(words[k]["w"])], default=-1) + 1
        if 0 < tail < len(words) and (len(words) - tail) <= ELLIPSIS_MAX_WORDS \
                and (not marks or marks[-1] < tail):
            marks.append(tail)
    return marks


# Bump when anything that changes the OUTPUT of build_takes changes, so a stale
# cache from an older algorithm can never be served.
CACHE_VERSION = 13


def _cache_sig(pdir: Path, src, tpath):
    """Everything that can change the answer: the media, the transcript, the code."""
    try:
        media = resolve(pdir, src.get("path"))
        ms = Path(media).stat() if media and Path(media).is_file() else None
        ts = (pdir / tpath).stat()
        return {"v": CACHE_VERSION, "media": [ms.st_size, int(ms.st_mtime)] if ms else None,
                "tx": [ts.st_size, int(ts.st_mtime)]}
    except OSError:
        return None


def build_takes(pdir: Path, src_key="main", use_cache=True):
    """Segment the source into takes. Cached on (media, transcript, algorithm):
    a full run decodes the audio and samples faces for ~33s, and a single AI edit
    asks for it two or three times — that was a minute of pure recomputation of
    an identical answer on every edit."""
    project = json.loads((pdir / "project.json").read_text())
    src = (project.get("sources") or {}).get(src_key) or {}
    tpath = src.get("transcript")
    if not (tpath and (pdir / tpath).exists()):
        # same convention fallback the server uses: the file may be on disk with
        # the project.json pointer lost to a stale autosave
        conv = f"media/transcript_{src_key}.json"
        tpath = conv if (pdir / conv).exists() else None
    if not tpath:
        raise SystemExit(f"source '{src_key}' has no transcript — transcribe first")
    cache = pdir / "media" / f"_takes_{src_key}.json"
    sig = _cache_sig(pdir, src, tpath)
    if use_cache and sig:
        try:
            hit = json.loads(cache.read_text())
            if hit.get("sig") == sig:
                return hit["data"]
        except (OSError, ValueError, KeyError):
            pass
    raw_words = json.loads((pdir / tpath).read_text()).get("words") or []
    if not raw_words:
        raise SystemExit("empty transcript")

    # The AUDIO has to be read before the transcript is cleaned: deciding whether
    # an impossible-rate burst is a hallucination or a real retake with a broken
    # clock is only answerable by asking whether there is speech in the recording
    # that no surviving word accounts for.
    media = resolve(pdir, src.get("path"))
    tmp = pdir / "media" / f"_takes_{os.getpid()}.wav"
    samples, sr = _load_audio(media, tmp)
    try:
        tmp.unlink()
    except OSError:
        pass
    env = envelope(samples, sr)
    thresh, floor = threshold_db(env)
    media_dur = float(src.get("duration") or 0) or media_duration(media)
    raw_end = max(w["t1"] for w in raw_words) + 1.0
    if media_dur:
        raw_end = min(raw_end, media_dur)
    audio_runs = speech_runs(env, thresh, raw_end) if env is not None else None

    words, hallucinated = sanitize(raw_words, audio_runs)
    if not words:
        raise SystemExit("transcript is entirely hallucinated — re-transcribe")

    end_all = max(w["t1"] for w in words) + 1.0
    if media_dur:
        end_all = min(end_all, media_dur)
    dur_cap = media_dur or end_all

    if env is None:  # no audio decode (no numpy / broken file) — one take
        runs = [[0.0, end_all]]
    else:
        runs = speech_runs(env, thresh, end_all) or [[0.0, end_all]]
    # silences = the complement, used to place a cut inside a group
    sils = [[a[1], b[0]] for a, b in zip(runs, runs[1:])]

    # 1) assign each word to a speech run. A word whose midpoint drifts into a
    #    silence must NOT be dropped (losing it can delete a whole script line),
    #    so fall back to most overlap, then nearest.
    buckets = [[] for _ in runs]
    for w in words:
        mid = (w["t0"] + w["t1"]) / 2
        idx = next((i for i, (a, b) in enumerate(runs) if a - 0.25 <= mid <= b + 0.25), None)
        if idx is None:
            ov = [(min(b, w["t1"]) - max(a, w["t0"]), i) for i, (a, b) in enumerate(runs)]
            best = max(ov, default=(0, None))
            idx = best[1] if best[0] > 0 else min(
                range(len(runs)),
                key=lambda i: min(abs(mid - runs[i][0]), abs(mid - runs[i][1])),
                default=None)
        if idx is not None:
            buckets[idx].append(w)
    for b in buckets:
        b.sort(key=lambda x: x["t0"])
    pairs = [(r, ws) for r, ws in zip(runs, buckets) if ws]
    if not pairs:
        raise SystemExit("no transcript word landed on audible speech")

    # 2) SPLIT vs MERGE, decided by the text (REN-135/136).
    #    The old rule was "pause shorter than 0.38s → merge", which merged four
    #    attempts at the same line into one take AND cut a line in half at a
    #    pause the speaker meant to be there. Length of the pause is not the
    #    signal; what he SAYS after it is.
    groups = []   # {"in","out","words","cont","aborted"}

    def close(g):
        if g["words"]:
            groups.append(g)

    cur = {"in": pairs[0][0][0], "out": pairs[0][0][1], "runs": 1,
           "words": list(pairs[0][1]), "cont": False, "aborted": False}
    for (reg, ws) in pairs[1:]:
        gap = reg[0] - cur["out"]
        if gap >= HARD_GAP:
            close(cur)
            cur = {"in": reg[0], "out": reg[1], "words": list(ws), "runs": 1,
                   "cont": False, "aborted": False}
        elif ends_sentence(" ".join(w["w"] for w in cur["words"])):
            # A new sentence after a short pause: keep it a separate take so the
            # AI can drop a single line, but mark it a CONTINUATION — if both get
            # picked, assemble joins them and the pause he meant to leave stays.
            close(cur)
            cur = {"in": reg[0], "out": reg[1], "words": list(ws), "runs": 1,
                   "cont": True, "aborted": False}
        elif (cur["runs"] == 1
              and cur["out"] - cur["in"] <= FALSE_LEAD_MAX
              and gap >= FALSE_LEAD_GAP
              and (reg[1] - reg[0]) >= (cur["out"] - cur["in"]) * FALSE_LEAD_RATIO):
            # short burst → pause → a far longer delivery: he restarted. Split it
            # off and flag it, or the finished video opens on the stumble.
            cur["aborted"] = True
            close(cur)
            cur = {"in": reg[0], "out": reg[1], "words": list(ws), "runs": 1,
                   "cont": False, "aborted": False}
        else:
            # mid-sentence pause (a breath, or a beat he is holding) — same take
            cur["out"] = reg[1]
            cur["words"] += ws
            cur["runs"] += 1
    close(cur)

    # 2b) split every group at its false starts. This runs on the TEXT, so it
    #     catches attempts the audio pass merged (whisper's clock drifts enough
    #     that three retries of one line landed inside a single speech run).
    split = []
    for g in groups:
        marks = find_restarts(g["words"])
        if not marks:
            split.append(g)
            continue
        # `carry` is the first word not yet emitted. When a boundary comes out
        # degenerate (whisper's stamps for this group sit outside its audible
        # span, so the clamp collapses lo and hi onto each other) the words must
        # ROLL INTO THE NEXT PART, never be skipped: dropping them deleted real
        # speech from every take while the next take's clip still played its
        # audio, and the self-check could not see it.
        lo, cont, carry = g["in"], g["cont"], 0
        bounds = marks + [len(g["words"])]
        for k, m in enumerate(bounds):
            last = k == len(bounds) - 1
            hi = (g["out"] if last else
                  _split_time(g["words"][m - 1], g["words"][m], sils,
                              lo + 0.05, g["out"] - 0.05, env))
            if not last and hi - lo < 0.05:
                continue                       # unusable boundary — keep accruing
            part = g["words"][carry:m]
            if not part:
                continue
            split.append({"in": lo, "out": max(hi, lo + 0.05), "words": part,
                          "cont": cont,
                          # anything that ends on a restart mark was abandoned
                          "aborted": not last})
            carry, lo, cont = m, hi, False
    groups = [g for g in split if g["words"]]

    # The head of an abandoned attempt, when the giving-up happened in the NEXT
    # group: "…de vendas." | "Enquanto" | "vender…". The middle group ends no
    # sentence and is a scrap, and what follows it was abandoned — so it is the
    # start of that same abandoned line, not content.
    for a, b in zip(groups, groups[1:]):
        if (b.get("aborted") and not a.get("aborted") and len(a["words"]) <= 3
                and not ends_sentence(" ".join(w["w"] for w in a["words"]))):
            a["aborted"] = True

    # a trailing unfinished clause that nothing restarted is still a false start
    for g in groups:
        if not g["aborted"] and trails_off(" ".join(w["w"] for w in g["words"])):
            g["aborted"] = True

    # 3) boundaries: run edges are already the real edges of the voice, so the
    #    pad is only anti-click. Clamp against neighbours so takes never overlap.
    takes = []
    for i, g in enumerate(groups):
        # Pin the group to the VOICE inside it. A boundary that came from a text
        # split lands in the MIDDLE of the silence between two attempts, so the
        # take that follows opened with half that pause as dead air (0.43s on a
        # real one). max/min make this safe in both directions: when a run starts
        # before the group — several attempts inside one continuous run — the
        # group's own edge wins and nothing moves.
        inner = [r for r in runs if r[1] > g["in"] + 0.01 and r[0] < g["out"] - 0.01]
        if inner:
            g["in"] = max(g["in"], inner[0][0])
            g["out"] = min(g["out"], inner[-1][1])
        prev_out = takes[-1]["out"] if takes else 0.0
        next_in = groups[i + 1]["in"] if i + 1 < len(groups) else end_all
        t_in = max(g["in"] - LEAD_PAD, prev_out + 0.01, 0.0)
        t_out = min(g["out"] + TAIL_PAD, next_in - 0.01, dur_cap)
        t_out = max(t_out, t_in + 0.05)
        text = " ".join(w["w"] for w in g["words"])
        first, last = g["words"][0], g["words"][-1]
        takes.append({
            "id": i + 1,
            "in": round(t_in, 3), "out": round(t_out, 3), "dur": round(t_out - t_in, 3),
            "text": text,
            "words": len(g["words"]),
            # source-time of every word in this take, so the assembler can trim
            # the clip to the approved script line (see assemble.trim_to_line)
            "wt": [[round(w["t0"], 3), round(w["t1"], 3), w["w"]] for w in g["words"]],
            # where the VOICE actually starts and stops inside this take, so a
            # later trim can snap onto it instead of trusting whisper's clock
            "sruns": [[round(a, 3), round(b, 3)] for a, b in runs
                      if b > t_in - 0.05 and a < t_out + 0.05],
            "continues": bool(g["cont"]),
            "aborted": bool(g["aborted"]),
            "db": _rms_db(samples, sr, first["t0"], last["t1"]),
            "wps": round(len(g["words"]) / max(0.1, last["t1"] - first["t0"]), 2),
            "ends_sentence": ends_sentence(text),
        })

    # 4) VISION — loudness alone can't tell a real take from a rehearsal spoken
    #    loudly. Framing does: in a rehearsal he drifts out of position.
    add_vision(pdir, media, takes)

    # 5) delivery flags
    dbs = sorted(t["db"] for t in takes if t["db"] is not None)
    median_db = dbs[len(dbs) // 2] if dbs else None
    for i, t in enumerate(takes):
        flags = []
        if t["aborted"]:
            nxt = next((o["id"] for o in takes[i + 1:]
                        if not o["aborted"] and o["words"] > 3
                        and not (median_db is not None and o["db"] is not None
                                 and o["db"] - median_db <= -QUIET_DB)), None)
            flags.append("FALSE START — he stopped and went again"
                         + (f", use take {nxt} instead" if nxt else ""))
        if median_db is not None and t["db"] is not None:
            t["db_rel"] = round(t["db"] - median_db, 1)
            if t["db_rel"] <= -QUIET_DB:
                flags.append("QUIET — likely a rehearsal/mumble, avoid")
        if t.get("framing") is not None and t["framing"] >= FRAMING_BAD:
            flags.append("OFF-FRAME — he is out of position (not addressing "
                         "the camera), avoid")
        if t.get("face_pct") is not None and t["face_pct"] < 0.5:
            flags.append("no face on camera for most of it")
        if not t["ends_sentence"] and not t["aborted"]:
            flags.append("cut off — does not finish the thought")
        if t["words"] <= 3 and not t["aborted"]:
            flags.append("fragment")
        t["flags"] = flags

    # 6) group repeated attempts of the same line (first words as signature)
    sigs = {}
    for t in takes:
        key = " ".join(_norm(t["text"]).split()[:5])
        sigs.setdefault(key, []).append(t["id"])
    for ids in sigs.values():
        if len(ids) > 1:
            for t in takes:
                if t["id"] in ids:
                    t["repeat_of"] = ids

    # self-check: a segmentation that lost words or produced overlaps is a
    # failure, not a result — say so instead of handing back a broken table
    kept = sum(t["words"] for t in takes)
    issues, notes = [], []
    if hallucinated:
        notes.append(f"{len(hallucinated)} of {len(raw_words)} transcript words were "
                     "dropped as whisper hallucinations (they had no audio of their own)")
    if kept < len(words) * 0.98:
        issues.append(f"{len(words) - kept} of {len(words)} transcript words fell outside every take")
    bad = [(a["id"], b["id"]) for a, b in zip(takes, takes[1:]) if b["in"] < a["out"] - 1e-6]
    if bad:
        issues.append(f"{len(bad)} overlapping takes ({bad[:3]})")
    if any(t["out"] - t["in"] <= 0 for t in takes):
        issues.append("a take has zero or negative length")

    data = {"src": src_key, "median_db": median_db, "issues": issues, "notes": notes,
            "hallucinated": len(hallucinated),
            "noise_floor_db": round(floor, 1) if floor is not None else None,
            "silence_threshold_db": round(thresh, 1), "takes": takes}
    if sig:
        try:
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sig": sig, "data": data}, ensure_ascii=False))
            os.replace(tmp, cache)
        except OSError:
            pass
    return data


def as_table(data):
    """Compact text table — this is what the AI reads to pick take numbers."""
    lines = [f"TAKES (source '{data['src']}', {len(data['takes'])} total; "
             f"session median loudness {data['median_db']} dB; "
             f"'frame' is how far the speaker is from this video's normal "
             f"framing — high means out of position).",
             "Never pick a take flagged FALSE START. A take marked "
             "'continues' runs on from the one before it: if you pick both, "
             "they are joined with no cut and the pause between them is kept.",
             *[f"NOTE: {n}" for n in data.get("notes") or []],
             "id | in → out | dur | loudness/framing | notes | text"]
    for t in data["takes"]:
        rel = f"{t.get('db_rel', 0):+.1f}dB" if t.get("db_rel") is not None else "?"
        fr = f" frame:{t['framing']:.2f}" if t.get("framing") is not None else ""
        rep = f" repeats:{t['repeat_of']}" if t.get("repeat_of") else ""
        cont = " continues" if t.get("continues") else ""
        notes = "; ".join(t["flags"]) or "ok"
        lines.append(f"{t['id']} | {t['in']:.2f} → {t['out']:.2f} | {t['dur']:.2f}s | "
                     f"{rel}{fr}{rep}{cont} | {notes} | {t['text']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--src", default="main")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    data = build_takes(Path(args.project_dir), args.src)
    if args.json:
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(as_table(data))


if __name__ == "__main__":
    main()
