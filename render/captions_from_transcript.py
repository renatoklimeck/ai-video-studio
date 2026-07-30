"""Generate word-synced captions DIRECTLY from the source transcript(s) —
no second whisper pass (REN-127). Captions are source-anchored, so the words a
clip keeps ARE the caption words: instant and exactly in sync with the audio.

Usage: python captions_from_transcript.py <project_dir>
Replaces auto captions (keeps hand-edited ones, marked "edited", and never
re-emits words inside an edited group's span). Prints "done: N captions".
"""
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from transcribe import CAPTION_STYLE, MAX_CHARS, MAX_GAP
from takes import sanitize
from common import load_project

# Renato's caption style: no punctuation on screen. This strips the DISPLAY text
# only — `ends_line` still reads the original spelling, so losing the full stop
# here can never cost us the line break it implies.
LEAD_PUNCT = "\u00bf\u00a1\"'([{\u2014-"
TAIL_PUNCT = ".,;:!?\u2026\"')]}\u2014-"


def shown(w):
    out = (w or "").strip().lstrip(LEAD_PUNCT).rstrip(TAIL_PUNCT).strip()
    return out or (w or "")


def ends_line(w):
    """A sentence ends here, so the next word starts a NEW caption line.
    Without this a line reads "ainda nem existe. E" and the next one opens with
    "depois de viver com a" — the connective marooned on the wrong line."""
    return (w or "").strip().endswith((".", "!", "?", "\u2026"))


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: captions_from_transcript.py <project_dir>")
    pdir = Path(sys.argv[1])
    project = load_project(pdir / "project.json")
    clips = project.get("clips") or []
    if not clips:
        sys.exit("no clips")

    # per-source transcript words — FRESH only (src_size must match the media
    # file when present; a stale transcript is another video's words)
    tx = {}
    for key, s in (project.get("sources") or {}).items():
        tp = s.get("transcript")
        if not (tp and (pdir / tp).exists()):
            conv = f"media/transcript_{key}.json"
            if not (pdir / conv).exists():
                continue
            tp = conv
        try:
            data = json.loads((pdir / tp).read_text())
        except (OSError, ValueError):
            continue
        sig = data.get("src_size")
        p = s.get("path") or ""
        media = Path(p) if p.startswith("/") else pdir / p
        if sig is not None and media.is_file() and media.stat().st_size != sig:
            continue  # stale
        # Same sanitiser the take detector uses. Without it whisper's loops ride
        # straight into the captions: this project put four words on screen that
        # were never spoken, stamped inside a 0.08s window.
        tx[key] = sanitize(data.get("words") or [])[0]
    if not tx:
        sys.exit("no fresh source transcript — transcribe first")

    # spans of hand-edited groups (kept as-is): never re-emit words inside them,
    # or the auto copy would stack on top and hide the user's edit
    kept = [c for c in project.get("captions", []) if c.get("edited")]
    edited_spans = {}
    for g in kept:
        ws = g.get("words") or []
        if ws:
            edited_spans.setdefault(g.get("src", "main"), []).append((ws[0]["t0"], ws[-1]["t1"]))

    def in_edited(src, mid):
        return any(a - 1e-3 <= mid <= b + 1e-3 for a, b in edited_spans.get(src, []))

    # Which clip owns each word — decided by how much of the word's span the clip
    # actually covers, NOT by its midpoint.
    #
    # takes.py places cuts on the audio envelope and deliberately tolerates
    # whisper's clock drift when it assigns a word to a take. This file used to
    # be stricter (midpoint must be inside the clip), so the cut was built from
    # one word list and the caption from a smaller one: whisper stamps a word's
    # t0 at the END of the previous word, i.e. back in the silence before the
    # take, so the OPENING word of a line kept falling out. On his own video that
    # deleted "Não" from "Não parece sucesso de verdade", inverting the sentence
    # on screen. Overlap wins for the clip that holds most of the word, so a word
    # spanning a cut is still emitted exactly once.
    owner = {}
    for src, ws in tx.items():
        for i, w in enumerate(ws):
            best, best_ov = None, 0.0
            for ci, c in enumerate(clips):
                if c.get("src", "main") != src:
                    continue
                ov = min(w["t1"], c["out"]) - max(w["t0"], c["in"])
                # A word must be MOSTLY inside the clip to be its caption. Any
                # positive overlap was enough before, so a word whose last 0.02s
                # of 0.82s crossed the cut ("te…", the tail of the attempt this
                # take replaces) printed on screen for a take that never says it.
                # Enough of the word must be inside to be heard there: half of
                # it, OR 120ms, whichever is less. Demanding a full half alone
                # threw away legitimate OPENING words, whose t0 whisper stamps
                # back in the silence before the take ("Agora,", "E sim"); a bare
                # sliver alone let a word 2.4% inside ("te…") print on screen.
                if ov > best_ov and ov >= min((w["t1"] - w["t0"]) * 0.5, 0.12):
                    best, best_ov = ci, ov
            if best is None:      # no overlap at all — fall back to the midpoint
                mid = (w["t0"] + w["t1"]) / 2
                best = next((ci for ci, c in enumerate(clips)
                             if c.get("src", "main") == src
                             and c["in"] - 1e-3 <= mid <= c["out"] + 1e-3), None)
            if best is not None:
                owner[(src, i)] = best

    new_caps = []
    for ci, c in enumerate(clips):  # timeline order; words stay in source time
        src = c.get("src", "main")
        words = []
        for i, w in enumerate(tx.get(src, [])):
            # Keyed by INDEX, never by timestamp: whisper routinely stamps two or
            # three DIFFERENT consecutive words with the same t0, and a t0-keyed
            # guard deleted every one after the first ("Porque a ambição" shipped
            # as "Porque").
            if owner.get((src, i)) != ci:
                continue
            mid = (w["t0"] + w["t1"]) / 2
            if in_edited(src, mid):
                continue
            # clamp to the clip so a boundary word never bleeds across the cut
            t0 = max(w["t0"], c["in"])
            t1 = min(w["t1"], c["out"])
            if t1 - t0 < 0.02:
                t1 = min(t0 + 0.05, c["out"])
            words.append({"w": shown(w["w"]), "raw": w["w"], "t0": t0, "t1": t1})
        # group into short karaoke lines: break on length or on a silence gap
        cur = []
        def flush():
            if cur:
                new_caps.append({
                    "id": "g" + uuid.uuid4().hex[:7],
                    **CAPTION_STYLE, "capAnchor": "source", "src": src,
                    "words": [{"w": w["w"], "t0": round(w["t0"], 2), "t1": round(w["t1"], 2)}
                              for w in cur],
                })
        for w in words:
            if cur:
                line = " ".join(x["w"] for x in cur)
                if (len(line) + 1 + len(w["w"]) > MAX_CHARS
                        or w["t0"] - cur[-1]["t1"] > MAX_GAP
                        or ends_line(cur[-1].get("raw", cur[-1]["w"]))):
                    flush()
                    cur = []
            cur.append(w)
        flush()

    if not new_caps:
        # nothing covered (e.g. the clips' sources have no transcript) — keep
        # whatever captions exist rather than wiping them
        print("done: 0 captions (no words covered; kept existing captions)")
        return

    project["captions"] = kept + new_caps
    project["by"] = "claude"
    (pdir / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=1))
    print(f"done: {len(new_caps)} captions")


if __name__ == "__main__":
    main()
