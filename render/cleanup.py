"""FIRST PASS on raw footage: remove the junk and the dead air, keep every take.

The other automatic path (assemble.py) needs an approved script and answers the
question "which take carries line 3?". This one answers nothing — it is the
manual cleaning Renato does before he even starts choosing, and it must not
choose for him:

  * the empty space between takes disappears by construction — schema v2
    concatenates clips, so a gap that is inside no clip is simply gone;
  * false starts, orphaned heads of an abandoned line and inaudible whisper
    ghosts are dropped;
  * EVERY complete attempt survives, in recording order. Five good reads of one
    line stay five clips, grouped and labelled "take 2 of 5" so he can compare
    them and delete four with two clicks.

He reviews the result by hand, so the two failures are not symmetric: leaving
junk costs him a click, deleting a good take loses material he may never notice
is missing. Every uncertain rule here therefore KEEPS and marks.

Usage: python cleanup.py <project_dir> [--src main] [--dry-run] [--force]
"""
import argparse
import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from takes import LEAD_PAD, TAIL_PAD, _norm, build_takes, ends_sentence  # noqa: E402

HERE = Path(__file__).parent

# --- what gets dropped -------------------------------------------------------
# A take this far under the session median with barely any duration is not a
# quiet delivery, it is whisper stamping words on near-silence: 4 words in 0.13s
# at -49 dB. Measured across three recordings the longest such take is 0.58s, so
# the duration guard costs nothing today and is what keeps this rule from ever
# reaching a real take. Loudness is NEVER a reason to drop on its own — QUIET
# fires at only -4 dB, which is him leaning back or an intimate line.
PHANTOM_DB = -20.0
PHANTOM_DUR = 0.6
# The head of an abandoned line ("Porque, mãe," | "gente" | "…"): a scrap that
# finishes no sentence and whose only possible continuation was itself given up.
ORPHAN_MAX_WORDS = 3
# …and it has to be the SAME line. Chained far enough, the rule walked back
# through four separate attempts spanning 25s and deleted the middle of one.
# The real "Porque, mãe," case sits 2.70s from the attempt it opened.
ORPHAN_GAP = 3.0

# EVERY rule below counts words, so every rule below is only as good as the
# transcript. Whisper does not fail loudly: over one 88-second stretch it
# collapsed the whole passage into a 44-word loop, stamping "Dois," across 4.6
# seconds and "uma maneira" across a 5.7-second complete delivery. Read as word
# counts those takes are two-word scraps, and the junk rules ate 11.5s of clean,
# in-frame, normal-volume speech — the only good reads of that section.
#
# The audio does not lie: divide the MEASURED voice inside a take by its word
# count. Real delivery across three recordings sits at 0.36 s/word (median) and
# never passes 0.82 in a recording whose transcript is sound; the collapsed
# stretch runs 1.9 to 2.9. Above the line the transcript is fiction, so nothing
# is dropped and nothing is trimmed — the take is kept and reported instead.
SPEECH_PER_WORD_MAX = 1.2
# The same lie told the other way round: sixteen words stamped on 0.17s of audio
# is 94 words a second. Nobody speaks at that rate, so the words are whisper's
# invention and what is under them is a scrap. It matters twice — such a take is
# junk, AND its text must never be matched against the script, because inventing
# a line is exactly how a 0.13s flash frame talked its way onto the timeline.
# Measured: real delivery here runs 2.8 words/second (median), 5.0 at the 90th.
MAX_WPS = 12.0

# --- trims -------------------------------------------------------------------
# Both trims only remove words the take before or after it already says. They
# reuse takes.py's pads so a cut still cannot land on the attack of a consonant.
TRIM_MIN = 0.05          # anything smaller is not worth a cut
HEAD_SEARCH = 5          # words to look into for the leftover sentence
HEAD_SNAP = 0.20         # snap the new head onto a real speech-run start
TAIL_MAX_WORDS = 3       # a longer trailing run is a thought, not a stump

# --- grouping repeated attempts ----------------------------------------------
SIG_N = 5                # words of head/tail signature
CHAIN_GAP = 2.5          # a longer pause is a new attempt, not the same sentence

# --- what the pass admits it did not handle ----------------------------------
UNCOVERED_MIN = 1.0      # speech this long that no take covers is worth saying

# --- the approved script -----------------------------------------------------
# The script is what makes this pass safe. Without it "was that attempt finished
# or abandoned?" is guesswork on audio shape; with it the question is arithmetic:
# how much of the line did this attempt actually deliver.
#
# It is the SAME script step as the full edit — he approves the text first. The
# only difference between the two passes is what happens next: the full edit
# keeps one attempt per line, this one keeps them all.
LINE_HIT = 0.34          # of a line's words, in order → this take is that line
COMPLETE_COVER = 0.80    # of a line's words → the attempt finished the thought
LOOKAHEAD = 3            # lines forward a take may jump (he skips, he re-orders)
# …and backward. A pointer that only advances cannot follow him restarting a
# line he already got part of: eleven takes of "Primeira, um produto tão bom…"
# were read against the NEXT line, scored 75% there instead of 100% here, and a
# word-perfect delivery was filed as an abandoned attempt.
LOOKBACK = 2

# --- the safety net ----------------------------------------------------------
# A cluster of dropped takes whose words survive nowhere else is a section he
# never landed. Dropping it is right; dropping it SILENTLY leaves a hole he may
# only find after he has stopped shooting.
CLUSTER_MIN_WORDS = 8
CLUSTER_MIN_SPAN = 15.0
CLUSTER_COVER = 0.5
# Below this share of the material surviving, assume the rules misfired rather
# than that the recording was that bad, and refuse to write anything.
KEEP_FLOOR = 0.55

DROP_LABEL = {
    "false-start": "false start (he stopped and went again)",
    "orphan-head": "head of a line he then abandoned",
    "inaudible": "inaudible — words stamped on near-silence",
    "phantom": "a flash of noise the transcript invented words for",
}


def _toks(text):
    return _norm(text or "").split()


def _starts_lower(word):
    for ch in word or "":
        if ch.isalpha():
            return ch.islower()
    return False


def voiced(t):
    """Seconds of MEASURED speech inside a take, from the audio envelope."""
    return sum(min(b, t["out"]) - max(a, t["in"])
               for a, b in (t.get("sruns") or [])
               if b > t["in"] and a < t["out"])


def transcript_ok(t):
    """False when this take has far more voice than its words can account for —
    whisper swallowed speech here, so no word-count rule may touch it."""
    if not t.get("words"):
        return True                      # nothing to reason about either way
    return voiced(t) / t["words"] <= SPEECH_PER_WORD_MAX


def text_real(t):
    """False when the words could not physically have been said in the audio
    that is there — the text is invented, so it proves nothing."""
    v = voiced(t)
    if not t.get("words") or v <= 0:
        return True
    return t["words"] / v <= MAX_WPS


def mark_drops(takes):
    """Which takes are junk. Returns ({take id: reason}, [suspect take ids]),
    biased towards keeping.

    Three signals only, all of them evidence that the take is not a delivery:
    he restarted, he abandoned the line this scrap opened, or there is no audio
    under the words. QUIET, OFF-FRAME, "cut off" and "fragment" are deliberately
    NOT here — see the module docstring.

    A take whose transcript is unreliable is exempt from all three: the signals
    are computed from words, and there the words are not what he said."""
    drop = {}
    suspect = [t["id"] for t in takes if not transcript_ok(t)]
    fiction = set(suspect)
    aborted = {t["id"]: bool(t.get("aborted")) for t in takes}
    for t in takes:
        if aborted[t["id"]] and t["id"] not in fiction:
            drop[t["id"]] = "false-start"

    # takes.py already has this rule (a <=3-word scrap followed by an abandoned
    # take is the head of that same abandoned line) but runs it as a single
    # forward pass, so it cannot walk back through a chain: by the time "gente"
    # is marked aborted, the pair ("Porque, mãe," , "gente") has already been
    # judged. Iterating to a fixed point catches the whole chain. It stays safe
    # because it never asks how long the pause was — only whether the one thing
    # that could have finished the fragment was itself given up.
    changed = True
    while changed:
        changed = False
        for a, b in zip(takes, takes[1:]):
            if (aborted[b["id"]] and not aborted[a["id"]]
                    and a["id"] not in fiction
                    and b["in"] - a["out"] <= ORPHAN_GAP
                    and a["words"] <= ORPHAN_MAX_WORDS and not a.get("ends_sentence")):
                aborted[a["id"]] = True
                drop[a["id"]] = "orphan-head"
                changed = True

    for t in takes:
        if t["id"] in drop or t["id"] in fiction:
            continue
        if t.get("db_rel") is not None and t["db_rel"] <= PHANTOM_DB \
                and t["dur"] <= PHANTOM_DUR:
            drop[t["id"]] = "inaudible"
        elif not text_real(t):
            drop[t["id"]] = "phantom"
    return drop, suspect


def trim_take(t, prev_kept=True):
    """Tighten a kept take to what it alone delivers. Returns (in, out, dropped
    word indices).

    He runs the tail of one attempt into the top of the next, and restarts the
    next line inside the current take and abandons it after a word or two — so a
    good sentence ships with a stump glued to either end. Both trims only remove
    words that a neighbouring take already carries, and both refuse when
    whisper's word clock puts them outside the take's own audio."""
    t_in, t_out = t["in"], t["out"]
    wt = t.get("wt") or []
    if len(wt) < 2 or not transcript_ok(t):
        return t_in, t_out, []      # the words are not what he said — do not cut
    dropped = []

    # HEAD: the take opens on the end of the previous attempt's sentence
    # ("pedir. Primeira, um produto tão bom…"). A lowercase first word is the
    # whole safety argument — a take that opens a real sentence starts with a
    # capital, so a genuine two-sentence take can never match.
    # …and only if that previous take is actually staying. The whole argument for
    # the head trim is that those words are DUPLICATED by the take before. When
    # that take was just dropped, they are not duplicated anywhere, and trimming
    # deleted the only surviving read of a line.
    if prev_kept and _starts_lower(wt[0][2]):
        for k in range(min(HEAD_SEARCH, len(wt) - 1)):
            if not ends_sentence(wt[k][2]):
                continue
            w0 = wt[k + 1][0]
            starts = [a for a, _b in (t.get("sruns") or []) if abs(a - w0) <= HEAD_SNAP]
            if not starts:
                # No silence there — the take is one continuous breath and the
                # "leftover sentence" only exists in the transcript. Cutting on
                # that evidence beheaded a complete take, shipping it starting
                # mid-sentence. A head trim now requires measured silence to
                # cut into; text alone is never enough.
                break
            cut = min(starts, key=lambda a: abs(a - w0)) - LEAD_PAD
            # Clamped, and it must MOVE. Whisper leaks a take's opening words
            # back into the previous take's audio (head words stamped 431.05s on
            # a take that starts at 433.83s); unclamped, that pulls the previous
            # attempt into this clip.
            if t_in + TRIM_MIN < cut < t_out - 0.10:
                t_in = round(cut, 3)
                dropped = list(range(k + 1))
            break

    # TAIL: he started the next line inside this take and gave up after a word
    # or two ("…de se tornar aquela pessoa. Você"). Cutting after a word whisper
    # marked with a full stop leaves the sentence complete either way.
    last = max((k for k in range(len(wt)) if ends_sentence(wt[k][2])), default=-1)
    if 0 <= last < len(wt) - 1 and (len(wt) - 1 - last) <= TAIL_MAX_WORDS:
        cut = min(t_out, wt[last][1] + TAIL_PAD)
        if cut > t_in + 0.10 and t_out - cut >= TRIM_MIN:
            t_out = round(cut, 3)
            dropped += list(range(last + 1, len(wt)))
    return t_in, t_out, dropped


def utterances(kept):
    """Chain kept takes into whole sentences.

    repeat_of signs a take by its first five words, which cannot see an attempt
    spread over three takes: "Você tem que acreditar" | "numa versão" | "de você
    que ainda nem existe." never matches its own retake "tem que" | "acreditar
    numa versão de você que ainda nem existe.". At sentence level the two share
    their last five words and group cleanly."""
    utts, cur = [], None
    for t in kept:
        if cur is not None and (t["in"] - cur["out"] > CHAIN_GAP or cur["ends"]):
            utts.append(cur)
            cur = None
        if cur is None:
            cur = {"takes": [], "toks": [], "in": t["in"], "out": t["out"], "ends": False}
        cur["takes"].append(t["id"])
        cur["toks"] += _toks(t["text"])
        cur["out"] = t["out"]
        cur["ends"] = bool(t.get("ends_sentence"))
    if cur is not None:
        utts.append(cur)
    return [u for u in utts if u["toks"]]


def group_attempts(utts):
    """Link utterances that are attempts at the same line. Returns {take id:
    "take 2 of 4"}.

    Head OR tail signature, both exact. They are complementary: a retake that
    adds words at the front only matches on the tail, one that gives up early
    only matches on the head. A fuzzy similarity fallback was tried and is not
    safe — at ratio 0.6 it collapsed thirteen unrelated sentences into one
    group."""
    parent = list(range(len(utts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for key in ("head", "tail"):
        seen = {}
        for i, u in enumerate(utts):
            n = min(SIG_N, len(u["toks"]))
            sig = tuple(u["toks"][:n] if key == "head" else u["toks"][-n:])
            if sig in seen:
                union(seen[sig], i)
            else:
                seen[sig] = i

    groups = {}
    for i in range(len(utts)):
        groups.setdefault(find(i), []).append(i)
    notes = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        for n, i in enumerate(sorted(members), 1):
            for tid in utts[i]["takes"]:
                notes[tid] = f"take {n} of {len(members)}"
    return notes, groups


def _longest_common_run(a, b):
    """Longest run of words the two share, in order."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def lost_sections(takes, drop, kept_utts):
    """Contiguous stretches of dropped takes whose words survive nowhere else.

    On a real recording this caught the second of four "engrenagens": fifteen
    takes over eighty seconds, every one of them a stub, and only four of its
    forty words spoken anywhere else. The rules are right to remove it — but
    removing it silently jumps the finished video from gear one to gear three."""
    runs, cur = [], []
    for t in takes:
        if t["id"] in drop:
            cur.append(t)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    kept_sigs = set()
    for u in kept_utts:
        n = min(SIG_N, len(u["toks"]))
        kept_sigs.add(tuple(u["toks"][:n]))
        kept_sigs.add(tuple(u["toks"][-n:]))

    out = []
    for run in runs:
        toks = [w for t in run for w in _toks(t["text"])]
        span = run[-1]["out"] - run[0]["in"]
        if len(toks) < CLUSTER_MIN_WORDS or span < CLUSTER_MIN_SPAN:
            continue
        n = min(SIG_N, len(toks))
        if tuple(toks[:n]) in kept_sigs or tuple(toks[-n:]) in kept_sigs:
            continue          # he re-delivered this line; the drop is invisible
        best = max((_longest_common_run(toks, _toks(t["text"]))
                    for t in takes if t["id"] not in drop), default=0)
        if best / len(toks) < CLUSTER_COVER:
            out.append({"takes": [t["id"] for t in run],
                        "in": run[0]["in"], "out": run[-1]["out"],
                        "words": len(toks), "covered": best,
                        "text": " ".join(t["text"] for t in run)[:80]})
    return out


def _sim(a, b):
    """Word similarity. He says "venda" where the approved line says "vende", and
    an exact test threw that whole line away (earcheck learned this the hard
    way)."""
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def _covered(take_toks, line_toks):
    """Which words of the line this take delivers. Returns the set of line
    indices matched.

    Aligned, not matched greedily. Greedy first-match reads the take left to
    right and never looks back, so a take that opens with two words the line
    does not have ("sem ambição, gente que não tem ambição…" against "Gente que
    não tem ambição…") burns its pointer on a stray match and scores 2 of 11 for
    a delivery that is word-perfect. Every line then looked unfinished."""
    if not take_toks or not line_toks:
        return set()
    # fold near-misses onto the line's own spelling first: he says "venda" where
    # the approved line says "vende", and the aligner below matches exactly
    canon = []
    for w in take_toks:
        if w in line_toks:
            canon.append(w)
            continue
        best = max(line_toks, key=lambda x: _sim(w, x))
        canon.append(best if _sim(w, best) >= 0.82 else w)
    hit = set()
    sm = difflib.SequenceMatcher(None, canon, line_toks, autojunk=False)
    for _a, b, size in sm.get_matching_blocks():
        hit.update(range(b, b + size))
    return hit


def attempts_from_script(takes, script_lines, text_of, heard_ids=()):
    """Read the takes against the approved script.

    Returns (attempts, line_of) where an attempt is a run of consecutive takes
    delivering one line, with the fraction of that line it got through. The
    whole point of this pass is what it does NOT do next: every attempt that
    finished its line survives, however many there are.

    He records in order, so the walk is a pointer that only moves forward, with
    a small look-ahead for the lines he skipped and came back to."""
    lines = [_toks(l) for l in script_lines]
    attempts, line_of, cur, ptr = [], {}, None, 0
    for t in takes:
        if not text_real(t) and t["id"] not in heard_ids:
            # invented words. Crediting them against a line is what let a 0.13s
            # flash claim it delivered a sentence and survive the cut.
            continue
        toks = _toks(text_of(t))
        best, best_hit, best_frac = None, set(), 0.0
        for li in range(max(0, ptr - LOOKBACK), min(len(lines), ptr + 1 + LOOKAHEAD)):
            if not lines[li]:
                continue
            hit = _covered(toks, lines[li])
            frac = len(hit) / len(lines[li])
            # ties go to the line he is closest to being on, so a take that says
            # two neighbouring lines equally well is credited where he is
            if frac > best_frac + 1e-9 or (abs(frac - best_frac) <= 1e-9 and best is not None
                                           and abs(li - ptr) < abs(best - ptr)):
                best, best_hit, best_frac = li, hit, frac
        if best is None or not best_hit:
            # a stumble or a scrap. It does NOT break the attempt in progress:
            # he clears his throat mid-sentence and finishes the line after, and
            # cutting the attempt in two there turns one complete delivery into
            # two unfinished ones.
            continue
        # A take can be a PIECE of a line — "Você tem que acreditar" | "numa
        # versão" | "de você que ainda nem existe." is one sentence in three
        # takes, and none of the three clears a coverage bar on its own. So the
        # bar applies to a take standing alone; a take that opens the line or
        # carries on from where the attempt stopped only has to match at all.
        opens = min(best_hit) <= 1
        carries = (cur is not None and cur["line"] == best
                   and min(best_hit) >= cur["max_idx"] - 1)
        if not (len(best_hit) >= LINE_HIT * len(lines[best])
                or (len(best_hit) >= 2 and (opens or carries))):
            continue
        line_of[t["id"]] = best
        # A take that goes back over words the running attempt already covered is
        # him starting the line again, not carrying on with it.
        restart = (cur is not None
                   and (cur["line"] != best or min(best_hit, default=0) < cur["max_idx"] - 1))
        if cur is None or restart:
            cur = {"line": best, "takes": [], "hit": set(), "max_idx": -1}
            attempts.append(cur)
        cur["takes"].append(t["id"])
        cur["hit"] |= best_hit
        cur["max_idx"] = max(cur["max_idx"], max(best_hit, default=-1))
        ptr = best
    for a in attempts:
        n = len(lines[a["line"]]) or 1
        a["cover"] = len(a["hit"]) / n
        a["complete"] = a["cover"] >= COMPLETE_COVER

    # Two short sentences said in one breath are one take and two script lines,
    # and the walk above can only credit the take to one of them. Left there the
    # other line looks like it was never delivered. Second pass: a line nobody
    # claimed is claimed by any take that says it whole.
    claimed = {a["line"] for a in attempts}
    for li, line in enumerate(lines):
        if li in claimed or not line:
            continue
        for t in takes:
            hit = _covered(_toks(text_of(t)), line)
            if len(hit) >= COMPLETE_COVER * len(line):
                attempts.append({"line": li, "takes": [t["id"]], "hit": hit,
                                 "max_idx": max(hit), "cover": len(hit) / len(line),
                                 "complete": True})
                break
    attempts.sort(key=lambda a: (a["takes"][0], a["line"]))
    return attempts, line_of


def build_clips(takes, drop, src_key):
    """Kept takes → clips, in recording order. Dead air is never emitted."""
    clips, trims, owner, last_tid = [], {}, {}, None
    prev_id = None
    for t in takes:
        tid = t["id"]
        if tid in drop:
            prev_id = tid
            continue
        t_in, t_out, lost = trim_take(t, prev_kept=(prev_id is not None
                                                    and prev_id not in drop))
        prev_id = tid
        if (t_in, t_out) != (t["in"], t["out"]):
            trims[tid] = (t_in, t_out, lost)
        # A take marked `continues` runs on from the one before it: he never
        # stopped, and the pause between them is delivery he meant to leave in.
        # Same rule assemble.py uses, minus nothing — but a head-trimmed take
        # must not be joined, or the stumble the trim found plays anyway.
        if clips and t.get("continues") and last_tid == tid - 1 and t_in <= t["in"] + 0.02:
            clips[-1]["out"] = t_out
            owner[tid] = len(clips) - 1
        else:
            clips.append({
                "id": f"c{len(clips) + 1}_{tid}",
                "src": src_key, "in": t_in, "out": t_out,
                "fadeIn": 0, "fadeOut": 0, "kfs": [], "bg": None, "rt": None,
            })
            owner[tid] = len(clips) - 1
        last_tid = tid
    return clips, trims, owner


def check(takes, drop, clips, trims, owner, src_dur):
    """Everything that means the result is not the recording minus its junk.
    These are refusals, not warnings: a broken timeline written over his project
    is worse than no timeline at all."""
    bad = []
    if not clips:
        bad.append("nothing survived — every take was classified as junk")
    for i, c in enumerate(clips, 1):
        if c["out"] - c["in"] <= 0:
            bad.append(f"clip {i} has zero or negative length")
    for a, b in zip(clips, clips[1:]):
        if b["in"] < a["out"] - 1e-6:
            bad.append(f"clips {a['id']} and {b['id']} overlap in the source")
    if clips and (clips[0]["in"] < -1e-6 or (src_dur and clips[-1]["out"] > src_dur + 0.05)):
        bad.append("a clip falls outside the source media")

    # No word may vanish by accident. Only the words a trim deliberately removed
    # (and the ones whisper already stamped outside their own take) may be
    # missing from the clip that carries the take.
    lost = []
    for t in takes:
        if t["id"] in drop:
            continue
        c = clips[owner[t["id"]]]
        cut = set(trims.get(t["id"], (0, 0, []))[2])
        for k, (w0, w1, word) in enumerate(t.get("wt") or []):
            mid = (w0 + w1) / 2
            if k in cut or not (t["in"] - 0.02 <= mid <= t["out"] + 0.02):
                continue
            if not (c["in"] - 0.02 <= mid <= c["out"] + 0.02):
                lost.append(f"take {t['id']} \"{word}\"")
    if lost:
        bad.append(f"{len(lost)} word(s) of kept takes fall outside their clip "
                   f"({', '.join(lost[:4])})")

    material = sum(t["dur"] for t in takes if not t.get("aborted"))
    keep = sum(c["out"] - c["in"] for c in clips)
    if material and keep < material * KEEP_FLOOR:
        bad.append(f"only {keep:.1f}s of {material:.1f}s of non-aborted speech survived "
                   f"({keep / material:.0%}) — the rules misfired, refusing to write")
    return bad


def _hand_edited(project, takes, clips, src_dur):
    """Has someone already cut this project by hand? Then a first pass would
    throw their work away."""
    old = project.get("clips") or []
    if not old:
        return False
    ok_in = {round(t["in"], 2) for t in takes} | {round(c["in"], 2) for c in clips} | {0.0}
    ok_out = {round(t["out"], 2) for t in takes} | {round(c["out"], 2) for c in clips}
    if src_dur:
        ok_out.add(round(src_dur, 2))
    near = lambda v, s: any(abs(v - x) <= 0.15 for x in s)  # noqa: E731
    return any(not (near(c.get("in", -1), ok_in) and near(c.get("out", -1), ok_out))
               for c in old)


def relisten(pdir, src_key, takes, ids):
    """Re-transcribe a few takes in ISOLATION, one whisper process each.

    Only for takes the audio says are lying: whisper collapsed a whole passage
    once, stamping "uma maneira" over a 5.7s complete delivery. Read as text that
    take is a two-word scrap and the junk rules eat it; heard on its own it is
    the only clean read of that section. Falls back to the transcript text if the
    ear is unavailable — never to a drop."""
    text = {}
    if not ids:
        return text
    try:
        from earcheck import _listen, _small_model  # noqa: PLC0415
        from renderer import resolve  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return text
    if _small_model() is None:
        return text
    project = json.loads((pdir / "project.json").read_text())
    src = (project.get("sources") or {}).get(src_key) or {}
    try:
        media = resolve(pdir, src.get("path"))
    except Exception:  # noqa: BLE001
        return text
    tmp = pdir / "media" / f"_relisten_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        by_id = {t["id"]: t for t in takes}
        for tid in ids:
            t = by_id.get(tid)
            if not t:
                continue
            try:
                # transcribe() narrates its progress on stdout; here that would
                # bury the report the caller actually reads
                with open(os.devnull, "w") as null:
                    old, sys.stdout = sys.stdout, null
                    try:
                        toks, _timed = _listen(media, t["in"], t["out"] - t["in"],
                                               tmp, f"t{tid}")
                    finally:
                        sys.stdout = old
            except Exception:  # noqa: BLE001
                continue
            if toks:
                text[tid] = " ".join(toks)
    finally:
        for f in tmp.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass
    return text


def uncovered_speech(pdir, src_key, takes):
    """Stretches of real speech that fall inside NO take, longest first.

    Nothing downstream can rescue these — they never reached the take table — so
    the only useful thing to do is say they exist."""
    try:
        from takes import _load_audio, envelope, speech_runs, threshold_db  # noqa: PLC0415
        from renderer import resolve  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    project = json.loads((pdir / "project.json").read_text())
    src = (project.get("sources") or {}).get(src_key) or {}
    tmp = pdir / "media" / f"_gap_{os.getpid()}.wav"
    try:
        samples, sr = _load_audio(resolve(pdir, src.get("path")), tmp)
        env = envelope(samples, sr)
        if env is None:
            return []
        thresh, _floor = threshold_db(env)
        runs = speech_runs(env, thresh, float(src.get("duration") or 0)
                           or max(t["out"] for t in takes))
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    spans = [(t["in"], t["out"]) for t in takes]
    out = []
    for a, b in runs or []:
        if b - a < UNCOVERED_MIN:
            continue
        if any(y > a + 0.05 and x < b - 0.05 for x, y in spans):
            continue
        out.append((a, b))
    out.sort(key=lambda r: r[0] - r[1])
    return out[:5]      # the worst few; a full list is noise, not information


def cleanup(pdir: Path, src_key="main", *, force=False, dry_run=False):
    data = build_takes(pdir, src_key)
    if data.get("issues"):
        # segmentation already knows it produced something unsound — cleaning on
        # top of that just launders the problem into a finished timeline
        return {"ok": False, "report": [],
                "problems": [f"take segmentation is unsound: {i}" for i in data["issues"]]}
    takes = data["takes"]
    if not takes:
        return {"ok": False, "report": [], "problems": ["no takes in this source"]}

    project = json.loads((pdir / "project.json").read_text())
    src = (project.get("sources") or {}).get(src_key) or {}
    src_dur = float(src.get("duration") or 0)

    drop, suspect = mark_drops(takes)

    # ---- the approved script decides what "finished" means ------------------
    script_lines = [l.strip() for l in (project.get("script") or "").splitlines() if l.strip()]
    heard = relisten(pdir, src_key, takes, suspect) if script_lines else {}

    def text_of(t):
        return heard.get(t["id"], t["text"])

    rescued, unfinished = [], []
    if script_lines:
        # PASS 1 — on the transcript, to find what it WOULD throw away.
        atts, _line_of = attempts_from_script(takes, script_lines, text_of, set(heard))
        done_lines = {a["line"] for a in atts if a["complete"]}
        doubt = {tid for a in atts if not a["complete"] and a["line"] in done_lines
                 for tid in a["takes"]}
        # PASS 2 — listen to every one of those before believing it.
        #
        # The transcript ROTATES a repeated read: whisper stamps the end of the
        # previous delivery onto the start of this take, so a word-perfect
        # re-read of "Me segue e eu te mostro…" arrives as "te mostro… Me segue
        # e eu" and scores 69% against its own line. Four complete reads in a
        # row were filed as abandoned that way. Heard on its own, each one is
        # obviously finished — so nothing is dropped on the transcript's word.
        if doubt - set(heard):
            heard.update(relisten(pdir, src_key, takes, sorted(doubt - set(heard))))
            atts, _line_of = attempts_from_script(takes, script_lines, text_of, set(heard))
        by_line = {}
        for a in atts:
            by_line.setdefault(a["line"], []).append(a)
        for line, group in by_line.items():
            done = [a for a in group if a["complete"]]
            for a in group:
                for tid in a["takes"]:
                    if a["complete"]:
                        # A finished attempt is never junk, whatever the audio
                        # heuristics thought. This is where five good reads of
                        # one line all survive — the pass does not choose.
                        if tid in drop:
                            drop.pop(tid)
                            rescued.append(tid)
                    elif done:
                        # abandoned, and he landed it later: that is the junk he
                        # cleans by hand. Only ever with a good one to fall back
                        # on — a line with no complete attempt keeps everything.
                        drop.setdefault(tid, "false-start")
            if not done:
                unfinished.append((line, script_lines[line]))

    clips, trims, owner = build_clips(takes, drop, src_key)
    kept = [t for t in takes if t["id"] not in drop]
    utts = utterances(kept)
    notes, groups = group_attempts(utts)
    holes = lost_sections(takes, drop, utts)

    problems = check(takes, drop, clips, trims, owner, src_dur)
    if problems:
        return {"ok": False, "problems": problems, "report": []}
    if _hand_edited(project, takes, clips, src_dur) and not force:
        return {"ok": False, "report": [], "problems": [
            "this project already has clips that do not line up with take "
            "boundaries — someone cut it by hand and a first pass would throw "
            "that away. Re-run with --force to replace it anyway."]}

    for tid in sorted(notes):       # take order, so a joined clip keeps its HEAD's label
        clips[owner[tid]].setdefault("note", notes[tid])

    # keep the creator's own per-clip work (background removal, retouch, zoom
    # keyframes, volume, fades) when a clip covers the same source range
    prior = {(round(c.get("in", -1), 2), c.get("src", "main")): c
             for c in (project.get("clips") or [])}
    for c in clips:
        old = prior.get((round(c["in"], 2), c["src"]))
        if old:
            for k in ("bg", "rt", "kfs", "vol", "fadeIn", "fadeOut"):
                if old.get(k) not in (None, [], 0):
                    c[k] = old[k]

    kept_dur = sum(c["out"] - c["in"] for c in clips)
    reasons = {}
    for r in drop.values():
        reasons[r] = reasons.get(r, 0) + 1
    by_id = {t["id"]: t for t in takes}
    trim_secs = sum((by_id[tid]["out"] - by_id[tid]["in"]) - (v[1] - v[0])
                    for tid, v in trims.items())
    joined = len(kept) - len(clips)
    report = [f"{len(takes)} takes in → {len(kept)} kept, {len(drop)} dropped"]
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        report.append(f"  dropped {n:>3}  {DROP_LABEL[r]}")
    if trims:
        report.append(f"  trimmed {len(trims):>3}  leftovers from the take before/after "
                      f"(-{trim_secs:.2f}s)")
    report.append(f"{len(clips)} clips" + (f" ({joined} continuation(s) joined with no cut)"
                                           if joined else ""))
    real_groups = sum(1 for m in groups.values() if len(m) > 1)
    if real_groups:
        report.append(f"{real_groups} line(s) recorded more than once — every attempt kept "
                      "and labelled, choose by hand")
    if src_dur:
        report.append(f"source {src_dur:.1f}s → timeline {kept_dur:.1f}s "
                      f"({src_dur - kept_dur:.1f}s of junk and dead air removed)")
    else:
        report.append(f"timeline {kept_dur:.1f}s")

    warn = []
    if not script_lines:
        warn.append("no approved script, so this ran on audio alone — approve the script "
                    "and run it again for a cleaner pass")
    if rescued:
        report.append(f"  rescued {len(rescued):>3}  attempts the transcript hid, confirmed by "
                      "listening to them on their own")
    for line, text in unfinished:
        warn.append(f"script line {line + 1} was never delivered all the way through — every "
                    f"attempt at it stopped short, and they were all kept: \"{text[:70]}\"")
    if suspect:
        span = sum(by_id[i]["dur"] for i in suspect)
        warn.append(f"{len(suspect)} take(s) kept without cleaning ({span:.1f}s, takes "
                    f"{', '.join(str(i) for i in suspect[:8])}"
                    f"{'…' if len(suspect) > 8 else ''}): the transcript there has far "
                    "less text than there is speech, so nothing here can tell junk from "
                    "delivery. Listen to those yourself.")
    # Speech the SEGMENTATION never saw, so no rule here could keep it. Measured
    # from the AUDIO — the transcript is the thing that failed, and a whole
    # finished sentence ("E sim, o Instagram é uma maneira de fazer isso.") was
    # missing from a real cut with nothing said about it.
    #
    # This cannot be read off the takes: a take's `sruns` are the runs that
    # overlap IT, so a run lying entirely between two takes appears in neither.
    # Decoding the envelope again costs 0.4s and is the only honest answer.
    for a, b in uncovered_speech(pdir, src_key, takes):
        warn.append(f"{a:.1f}-{b:.1f}s ({b - a:.1f}s) is speech that landed in no take at "
                    "all, so it is not on the timeline — listen and add it by hand if "
                    "you want it")
    for h in holes:
        warn.append(f"takes {h['takes'][0]}-{h['takes'][-1]} ({h['in']:.0f}-{h['out']:.0f}s) "
                    f"were all junk and these words are spoken nowhere else — that part of "
                    f"the video was never delivered cleanly: \"{h['text']}\"")
    for i, c in enumerate(clips, 1):
        if c["out"] - c["in"] < 0.35:
            warn.append(f"clip {i} is very short ({c['out'] - c['in']:.2f}s)")
    for k in ("texts", "overlays", "audios"):
        if project.get(k):
            warn.append(f"{len(project[k])} {k} are timed against the TIMELINE, which just "
                        "got shorter — check they still land where you want")

    if dry_run:
        return {"ok": True, "dry_run": True, "clips": len(clips), "captions": None,
                "duration": round(kept_dur, 2), "report": report, "warnings": warn,
                "captions_note": "(dry run — nothing written)"}

    project = json.loads((pdir / "project.json").read_text())  # freshest on disk
    project["clips"] = clips
    project["by"] = "claude"
    _write(pdir / "project.json", project)

    # captions LAST, from the transcript over the final cut. They are
    # source-anchored, so a caption for a clip he later deletes goes with it.
    cap = subprocess.run([sys.executable, str(HERE / "captions_from_transcript.py"), str(pdir)],
                         capture_output=True, text=True, timeout=300)
    caps_note = (cap.stdout or cap.stderr or "").strip().splitlines()[-1:] or [""]
    if cap.returncode != 0:
        warn.append(f"captions could not be regenerated ({caps_note[0][:80]}) — "
                    "the ones on the timeline are stale")

    final = json.loads((pdir / "project.json").read_text())
    return {"ok": True, "clips": len(final["clips"]),
            "captions": len(final.get("captions") or []),
            "duration": round(kept_dur, 2), "report": report, "warnings": warn,
            "captions_note": caps_note[0]}


def _write(path: Path, data):
    """tmp + replace: the browser autosaves this same file every 900ms, and a
    reader landing mid-write would see half a project."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--src", default="main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="replace a timeline that was already cut by hand")
    args = ap.parse_args()
    res = cleanup(Path(args.project_dir), args.src, force=args.force, dry_run=args.dry_run)
    print("\n".join(res.get("report") or []))
    if not res["ok"]:
        print("\nREJECTED — nothing was changed:")
        for p in res["problems"]:
            print("  •", p)
        sys.exit(2)
    if not res.get("dry_run"):
        print(f"{res['captions']} caption groups ({res['captions_note']})")
    for w in res.get("warnings") or []:
        print("  ⚠", w)


if __name__ == "__main__":
    main()
