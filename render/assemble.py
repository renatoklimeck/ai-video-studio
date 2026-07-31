"""Deterministic ASSEMBLY (REN-134): turn the AI's take PICKS into the timeline.

The model never writes a timestamp. It reads the take table (takes.py) and the
approved script and answers only "script line 3 → take 12". This module:

  * rebuilds clips straight from those takes' precomputed boundaries (which sit
    in real silence, so no cut lands mid-word);
  * refuses picks that are flagged as rehearsals unless explicitly forced;
  * regenerates captions LAST, from the transcript over the final cut, so the
    caption text and sync always match what is actually on the timeline;
  * validates the result and reports what it did.

Usage: python assemble.py <project_dir> --plan plan.json [--allow-quiet]
  plan.json = {"src": "main",
               "picks": [{"line": 1, "takes": [3]}, {"line": 2, "takes": [7, 8]}]}
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from takes import _norm, build_takes  # noqa: E402
from cleanup import _covered, _sim  # noqa: E402

HERE = Path(__file__).parent

# How far into a take to look for the script line's opening words, and the most
# a trim is allowed to shave off. Both are small on purpose: this is meant to
# remove a two-word stumble, not to re-cut the take.
TRIM_SEARCH = 8
# Still the ceiling for a TAIL trim. The head trim outgrew it: a take can open
# with eight seconds of the previous line, and what makes a long head cut safe is
# not a stopwatch but proof that the whole line survives it (COVER_KEEP).
TRIM_MAX = 3.0
TRIM_LEAD = 0.06
TRIM_TAIL = 0.10
# how much of the line must survive a head trim for that trim to be allowed
COVER_KEEP = 0.80


def trim_to_line(t, line):
    """Narrow a take to where the approved script line really starts and ends.

    The take detector already splits off whole retries, but it cannot see a
    mid-phrase self-correction ("…sem ambição, gente que não tem ambição não
    consegue…"): nothing restarts, so it is one continuous delivery. The
    approved script says which words belong, so the fix is to align against it
    and drop the stumble. Returns (in, out) — unchanged when unsure."""
    wt = t.get("wt") or []
    line_toks = _norm(line or "").split()
    if len(wt) < 3 or len(line_toks) < 3:
        return t["in"], t["out"]
    toks = [_norm(w[2]) for w in wt]
    t_in, t_out = t["in"], t["out"]

    # Where does this line REALLY start inside the take? Two things broke here,
    # and both shipped a wrong cut:
    #
    #   * exact matching. The script says "Eu vivo assim", he said "Eu vivo
    #     isso" — one word, and the trim silently did nothing, so the clip kept
    #     a stumble and then said the whole line a second time.
    #   * looking only at the first few words, and never cutting more than 3s.
    #     A take can open with the tail of the PREVIOUS line (8 seconds of it),
    #     and the real opening sits at word 19.
    #
    # So: search the whole take, fuzzily, and take the LAST opening that still
    # leaves the entire line behind it. Cutting can never eat the line itself —
    # that is what the coverage test guarantees — and the cut still has to land
    # on measured silence below.
    starts_at = _line_starts(toks, line_toks)
    head = line_toks[:3]
    fallback = [k for k in range(1, max(1, min(TRIM_SEARCH, len(toks) - 2)))
                if toks[k:k + 3] == head]
    for k in (starts_at or fallback):
        if k:
            cut = wt[k][0] - TRIM_LEAD
            prev_end = wt[k - 1][1] if k else wt[0][0]
            if prev_end < wt[k][0]:            # land in the gap when there is one
                cut = max(cut, (prev_end + wt[k][0]) / 2)
            # Snap onto the VOICE. Whisper's t0 runs early, so trimming to it
            # left up to 0.42s of silence at the head of a clip even though the
            # trim itself was right. The take carries its own speech runs.
            starts = [a for a, _b in (t.get("sruns") or []) if a >= cut - 0.25]
            if starts:
                cut = max(cut, min(starts) - TRIM_LEAD)
            if t["in"] < cut < t["out"] - 0.3:
                t_in = round(cut, 3)
            break

    # Only shorten the tail when there is demonstrably EXTRA speech after the
    # line (a stray word from the next thought). When the line's last words ARE
    # the take's last words, leave `out` alone: it came from the audio envelope
    # and is already tight, while whisper's final t1 often lands early and would
    # clip the word.
    tail = line_toks[-3:]
    for k in range(len(toks) - 4, max(len(toks) - TRIM_SEARCH - 3, -1), -1):
        if toks[k:k + 3] == tail:
            end = wt[k + 2][1]
            cut = min(end + TRIM_TAIL, (end + wt[k + 3][0]) / 2
                      if wt[k + 3][0] > end else end + TRIM_TAIL)
            # A tail trim may only land in SILENCE. Whisper heard "…e-book de
            # R$47 mil?" where the script says "…de R$47", so the trim cut at
            # word-end + 0.10s — which was inside one continuous speech run and
            # chopped the final word in half on screen. If the cut falls inside
            # a measured speech run, the extra words are not separable from the
            # line by a cut; keep the take's own audio-derived end instead.
            in_run = any(a + 0.02 < cut < b - 0.02 for a, b in (t.get("sruns") or []))
            if not in_run and t_out - TRIM_MAX <= cut < t_out:
                t_out = round(cut, 3)
            break
    return (t_in, t_out) if t_out - t_in > 0.3 else (t["in"], t["out"])


def _line_starts(toks, line_toks):
    """Every position where this line plausibly BEGINS inside the take, latest
    first, keeping only the ones that still leave the whole line behind them.

    Fuzzy on purpose (earcheck learned this the hard way): the approved line says
    "assim" where he said "isso", and an exact test found nothing at all — so a
    take that delivered the line twice, stumble and all, shipped from its first
    word. The coverage test is what makes a long cut safe: we never move past
    words the line still needs."""
    if len(line_toks) < 3 or len(toks) < 3:
        return []
    # Do not look for the opening WORDS. He re-says a line with an extra word in
    # front ("Eu vivo isso…" then "Eu JÁ vivo isso…") and any fixed-width match
    # on the first three tokens misses the second delivery entirely — which is
    # the one he meant to keep.
    #
    # Ask the only question that matters instead: how late can this clip start
    # without losing a single word of the line? Whatever sits before that point
    # is, by construction, not part of the line — a stumble, a repeat, or the
    # tail of the previous one.
    best = len(_covered(toks, line_toks))
    if best < COVER_KEEP * len(line_toks):
        return []                      # the take does not really carry this line
    lo, hi = 0, len(toks) - 1
    while lo < hi:                     # coverage only falls as k grows: bisect
        mid = (lo + hi + 1) // 2
        if len(_covered(toks[mid:], line_toks)) >= best:
            lo = mid
        else:
            hi = mid - 1
    # Belt and braces: the new head must BE the start of the line, not some word
    # in the middle of it. Without this, a line whose opening word he never said
    # could slide the cut inward and the clip would open mid-sentence.
    if lo > 0 and any(_sim(toks[lo], w) >= 0.7 for w in line_toks[:2]):
        return [lo]
    return []


def assemble(pdir: Path, plan: dict, allow_quiet=False, trusted=()):
    """`trusted` = take ids whose flags were CHECKED BY LISTENING and found wrong.

    The flags are heuristics over a transcript that lies. A take reading its line
    perfectly at normal volume was marked FALSE START because whisper bled the
    next take's words into it, and since it was the only take covering that line
    the whole edit dead-ended on "nothing was changed". A flag may refuse a take;
    it may not refuse it after the audio has been heard and disagrees."""
    trusted = set(trusted or ())
    src_key = plan.get("src") or "main"
    data = build_takes(pdir, src_key)
    if data.get("issues"):
        # segmentation already knows it produced something unsound — assembling
        # on top of that just launders the problem into a finished timeline
        return {"ok": False, "report": [],
                "problems": [f"take segmentation is unsound: {i}" for i in data["issues"]]}
    by_id = {t["id"]: t for t in data["takes"]}
    project = json.loads((pdir / "project.json").read_text())
    script_lines = [l for l in (project.get("script") or "").splitlines() if l.strip()]

    clips, report, problems, head_lines = [], [], [], []
    seen_takes, seen_lines, last_tid, clip_of = {}, [], None, {}
    raw_picks = plan.get("picks")
    if not isinstance(raw_picks, list):
        return {"ok": False, "problems": ["picks must be a list"], "report": []}
    for pick in raw_picks:
        if not isinstance(pick, dict):
            problems.append(f"malformed pick: {pick!r}")
            continue
        try:
            line_no = int(pick.get("line"))
        except (TypeError, ValueError):
            problems.append(f"pick without a valid line number: {pick!r}")
            continue
        raw_ids = pick.get("takes")
        if isinstance(raw_ids, (int, str)):
            raw_ids = [raw_ids]
        ids = []
        for v in (raw_ids or []):
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                problems.append(f"line {line_no}: take id {v!r} is not a number")
        if not ids:
            problems.append(f"line {line_no}: no take chosen")
            continue
        if line_no in seen_lines:
            problems.append(f"line {line_no} was assigned twice")
        seen_lines.append(line_no)
        for tid in ids:
            if tid in seen_takes:
                if seen_takes[tid] == line_no - 1 and len(ids) == 1 and tid in clip_of:
                    # same take carries this line too — one clip covers both.
                    # Re-trim its tail against the LATER line, or the trim made
                    # for the first line would cut this one off.
                    t = by_id[tid]
                    later = script_lines[line_no - 1] if 0 < line_no <= len(script_lines) else ""
                    _, t_out = trim_to_line(t, later)
                    clips[clip_of[tid]]["out"] = max(clips[clip_of[tid]]["out"], t_out)
                    seen_takes[tid] = line_no
                    report.append(f"  line {line_no} → take {tid} (same take as line "
                                  f"{line_no - 1}; one clip covers both)")
                    continue
                problems.append(f"take {tid} reused (lines {seen_takes[tid]} and {line_no})")
                continue
            seen_takes[tid] = line_no
            t = by_id.get(tid)
            if not t:
                problems.append(f"line {line_no}: take {tid} does not exist")
                continue
            if t.get("aborted") and tid not in trusted:
                problems.append(
                    f"line {line_no}: take {tid} is a FALSE START "
                    f"(\"{t['text'][:40]}\") — he stopped and went again, use a later take")
                continue
            quiet = any("QUIET" in f for f in t["flags"])
            if quiet and not allow_quiet and tid not in trusted:
                problems.append(
                    f"line {line_no}: take {tid} is flagged as a rehearsal "
                    f"({t.get('db_rel')}dB below the session) — pick another")
                continue
            full_line = script_lines[line_no - 1] if 0 < line_no <= len(script_lines) else ""
            line_txt = full_line[:48] or "?"
            # only the take that carries the line can be trimmed against it —
            # a line split across two takes has no single text to align to
            t_in, t_out = (trim_to_line(t, full_line) if len(ids) == 1
                           else (t["in"], t["out"]))
            trimmed = ("" if (t_in, t_out) == (t["in"], t["out"])
                       else f" trimmed -{(t_in - t['in']) + (t['out'] - t_out):.2f}s")
            # A take marked `continues` runs on from the one before it: he never
            # stopped, the pause between them is delivery he meant to leave in
            # (REN-136). If both were picked, extend instead of cutting — a cut
            # there removes a beat that is part of the video.
            # Joining throws the trimmed IN point away (the join keeps playing
            # from the previous clip), so a take that needs its head trimmed must
            # NOT be joined — otherwise the stumble the trim found stays in the
            # video while the report claims it was removed.
            head_trimmed = t_in > t["in"] + 0.02
            if clips and t.get("continues") and last_tid == tid - 1 and not head_trimmed:
                pause = t["in"] - clips[-1]["out"]
                clips[-1]["out"] = t_out
                report.append(f"  line {line_no} → take {tid} JOINED to take {last_tid} "
                              f"(no cut, {pause:.2f}s pause kept){trimmed}  \"{line_txt}\"")
            else:
                clips.append({
                    "id": f"c{len(clips) + 1}_{tid}",
                    "src": src_key, "in": t_in, "out": t_out,
                    "fadeIn": 0, "fadeOut": 0, "kfs": [], "bg": None, "rt": None,
                })
                head_lines.append(full_line)
                report.append(f"  line {line_no} → take {tid} "
                              f"[{t_in:.2f}→{t_out:.2f}] {t.get('db_rel', 0):+.1f}dB"
                              f"{trimmed}  \"{line_txt}\"")
            clip_of[tid] = len(clips) - 1
            last_tid = tid

    # every approved script line must be covered exactly once — a plan that
    # silently drops lines is the "faltou conteúdo" failure all over again
    if script_lines:
        missing = [i for i in range(1, len(script_lines) + 1) if i not in seen_lines]
        if missing:
            problems.append(f"script lines with no take: {missing}")
    if seen_lines != sorted(seen_lines):
        problems.append("picks are out of script order")
    # Takes are numbered in recording order, so picks must climb too. Without
    # this, lines 1/2/3 mapped to takes 3/2/1 assembled with no complaint and
    # the finished video played its own content backwards.
    order = list(seen_takes)
    back = sum(1 for a, b in zip(order, order[1:]) if b < a)
    order_warn = None
    if back and back >= max(2, len(order) // 3):
        problems.append(f"takes are out of recording order ({order[:6]}) — the video "
                        "would play its parts in the wrong sequence")
    elif back:
        order_warn = (f"{back} pick(s) step back to an earlier take — fine when that "
                      "take is the only one carrying the line, worth a look otherwise")
    if problems:
        return {"ok": False, "problems": problems, "report": report}
    if not clips:
        return {"ok": False, "problems": ["no clips produced"], "report": report}

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

    project = json.loads((pdir / "project.json").read_text())  # freshest on disk
    project["clips"] = clips
    project["by"] = "claude"
    (pdir / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=1))

    # EARS (earcheck): whisper's full pass fuses an aborted attempt with its
    # retake into one clean sentence, so no transcript reader can see the
    # stumble — but a re-listen of just the clip head hears it plainly. This
    # moves fused heads onto the real retake and re-stamps the transcript
    # windows so the captions below are in sync. Fail-open: any problem here
    # leaves the cut exactly as assembled.
    try:
        from earcheck import audit_heads, apply_patches  # noqa: PLC0415
        ear_rep, ear_patches = audit_heads(pdir, clips, head_lines)
        if ear_patches:
            apply_patches(pdir, src_key, ear_patches)
        if ear_rep:
            report += ear_rep
            project["clips"] = clips
            (pdir / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=1))
    except (Exception, SystemExit) as e:  # noqa: BLE001
        report.append(f"  ear: audit skipped ({e})")

    # captions LAST, from the transcript over the final cut
    cap = subprocess.run([sys.executable, str(HERE / "captions_from_transcript.py"), str(pdir)],
                         capture_output=True, text=True, timeout=300)
    caps_note = (cap.stdout or cap.stderr or "").strip().splitlines()[-1:] or [""]
    caps_failed = cap.returncode != 0

    # validate
    final = json.loads((pdir / "project.json").read_text())
    warn = []
    if order_warn:
        warn.append(order_warn)
    if caps_failed:
        warn.append(f"captions could not be regenerated ({caps_note[0][:80]}) — "
                    "the ones on the timeline are stale")
    elif not final.get("captions"):
        warn.append("no captions were produced for this cut")
    for i, c in enumerate(final["clips"], 1):
        if c["out"] - c["in"] < 0.35:
            warn.append(f"clip {i} is very short ({c['out'] - c['in']:.2f}s)")
    total = sum(c["out"] - c["in"] for c in final["clips"])
    return {"ok": True, "clips": len(final["clips"]), "captions": len(final.get("captions", [])),
            "duration": round(total, 2), "report": report, "warnings": warn,
            "captions_note": caps_note[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--allow-quiet", action="store_true")
    args = ap.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    res = assemble(Path(args.project_dir), plan, args.allow_quiet)
    print("\n".join(res.get("report") or []))
    if not res["ok"]:
        print("\nREJECTED — nothing was changed:")
        for p in res["problems"]:
            print("  •", p)
        sys.exit(2)
    print(f"\nOK: {res['clips']} clips, {res['duration']}s, {res['captions']} caption groups "
          f"({res['captions_note']})")
    for w in res.get("warnings") or []:
        print("  ⚠", w)


if __name__ == "__main__":
    main()
