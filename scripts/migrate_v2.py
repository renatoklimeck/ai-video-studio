"""Migrate a v1 project.json to schema v2 (see SCHEMA.md).

Key conversions:
  - width/height -> w/h + aspect
  - timeline[] -> clips[] (src="main", zoom -> kfs with cx/cy in % 0-100)
  - bg {enabled, previewCache, cachedIn/Out} -> bg|null with processed/stale + _cache
  - captions: words[{w,t0,t1}] in SOURCE time -> words[{w,t,d}] in TIMELINE time
    (monotonic clamped mapping: words falling in cut-out regions snap to the cut)
  - x/y/size/w/h fractions -> percentages; dimAlpha->dim; type->mode; gain->vol
  - overlays: type->kind, volume->vol, align->crop, name from path; .cutout. paths
    become cut=true with cutPath
  - audio[] -> audios[]

Usage: python scripts/migrate_v2.py <project_dir> [--dry]
Writes project.json in place (v1 backed up as project-v1-backup.json).
"""
import argparse
import json
import shutil
from pathlib import Path


def src_to_timeline(timeline, st):
    """Source seconds -> timeline seconds, monotonic + clamped.
    Times inside a clip map linearly; times in cut-out gaps snap to the cut point."""
    acc = 0.0
    for s in timeline:
        if st < s["in"]:
            return acc
        if st <= s["out"]:
            return acc + (st - s["in"])
        acc += s["out"] - s["in"]
    return acc


def pct(v, default=0.0):
    return round((v if v is not None else default) * 100, 2)


def migrate(p):
    w, h = p["width"], p["height"]
    tl = p.get("timeline", [])

    clips = []
    for s in tl:
        bg = s.get("bg") or {}
        nbg = None
        if bg.get("enabled"):
            cache = bg.get("previewCache")
            fresh = cache and bg.get("cachedIn") == s["in"] and bg.get("cachedOut") == s["out"]
            nbg = {
                "color": bg.get("color", "#000000"),
                "feather": bg.get("feather", 12),
                "image": bg.get("image"),
                "processed": bool(cache),
                "stale": bool(cache) and not fresh,
            }
            if cache:
                nbg["_cache"] = {"path": cache, "in": bg.get("cachedIn"), "out": bg.get("cachedOut")}
        clips.append({
            "id": s["id"], "src": "main",
            "in": s["in"], "out": s["out"],
            "fadeIn": s.get("fadeIn", 0) or 0, "fadeOut": s.get("fadeOut", 0) or 0,
            "kfs": [{"t": k["t"], "scale": k["scale"],
                     "cx": pct(k.get("cx", 0.5)), "cy": pct(k.get("cy", 0.5))}
                    for k in (s.get("zoom") or [])],
            "bg": nbg, "rt": None,
        })

    captions = []
    for c in p.get("captions", []):
        words = []
        for wd in c.get("words") or []:
            t = src_to_timeline(tl, wd["t0"]) if (c.get("anchor", "source") == "source") else wd["t0"]
            t1 = src_to_timeline(tl, wd["t1"]) if (c.get("anchor", "source") == "source") else wd["t1"]
            words.append({"w": wd["w"], "t": round(t, 3), "d": round(max(0.06, t1 - t), 3)})
        if not words:
            continue
        captions.append({
            "id": c["id"], "x": pct(c.get("x"), 0.5), "y": pct(c.get("y"), 0.6),
            "size": pct(c.get("size"), 0.022), "font": c.get("font", "Helvetica Bold"),
            "color": (c.get("color") or "#FFFFFF").upper(),
            "dim": c.get("dimAlpha", 0.55),
            "mode": "karaoke" if c.get("type", "karaoke") == "karaoke" else "static",
            "words": words,
        })

    texts = [{
        "id": x["id"], "text": x.get("text", ""),
        "x": pct(x.get("x"), 0.5), "y": pct(x.get("y"), 0.5),
        "size": pct(x.get("size"), 0.03), "color": (x.get("color") or "#FFFFFF").upper(),
        "t0": x.get("t0", 0), "t1": x.get("t1", 0),
        "fadeIn": x.get("fadeIn", 0) or 0, "fadeOut": x.get("fadeOut", 0) or 0,
        "shadow": x.get("shadow", True),
        "font": x.get("font", "Arial Rounded"),
    } for x in p.get("texts", [])]

    dur = sum(s["out"] - s["in"] for s in tl)
    overlays = []
    for o in p.get("overlays", []):
        cut = ".cutout." in (o.get("path") or "")
        overlays.append({
            "id": o["id"], "kind": "video" if o.get("type") == "video" else "image",
            "name": Path(o.get("path", "overlay")).name,
            "path": o.get("path"), "proxy": o.get("proxy"),
            "cutPath": o.get("path") if cut else None,
            "x": pct(o.get("x"), 0.5), "y": pct(o.get("y"), 0.5),
            "w": pct(o.get("w"), 0.3), "h": pct(o["h"]) if o.get("h") else None,
            "t0": o.get("t0", 0), "t1": o.get("t1") if o.get("t1") is not None else round(dur, 3),
            "fadeIn": o.get("fadeIn", 0) or 0, "fadeOut": o.get("fadeOut", 0) or 0,
            "cut": cut, "vol": o.get("volume", 0) or 0,
            "crop": "top" if o.get("align") == "top" else "center",
        })

    audios = [{
        "id": a["id"], "name": Path(a.get("path", "audio")).name, "path": a.get("path"),
        "t0": a.get("t0", 0), "t1": a.get("t1") if a.get("t1") is not None else round(dur, 3),
        "vol": a.get("gain", 1.0), "fadeIn": a.get("fadeIn", 0) or 0, "fadeOut": a.get("fadeOut", 0) or 0,
    } for a in p.get("audio", [])]

    return {
        "version": 2,
        "name": p.get("name", "Untitled project"),
        "w": w, "h": h,
        "aspect": "16:9" if w >= h else "9:16",
        "fps": p.get("fps", 30),
        "by": "claude",
        "sources": p.get("sources", {}),
        "clips": clips, "captions": captions, "texts": texts,
        "overlays": overlays, "audios": audios,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    d = Path(args.project_dir)
    f = d / "project.json"
    p = json.loads(f.read_text())
    if p.get("version") == 2:
        print("already v2, nothing to do")
        return
    v2 = migrate(p)
    if args.dry:
        print(json.dumps(v2, ensure_ascii=False, indent=1))
        return
    shutil.copy(f, d / "project-v1-backup.json")
    f.write_text(json.dumps(v2, ensure_ascii=False, indent=1))
    print(f"migrated {f} (backup: project-v1-backup.json)")


if __name__ == "__main__":
    main()
