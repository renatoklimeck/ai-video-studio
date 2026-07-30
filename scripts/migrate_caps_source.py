"""Migrate captions from TIMELINE-anchored ({w,t,d}) to SOURCE-anchored
(capAnchor:"source", src, words {w,t0,t1}) — REN-115.

Each caption word's timeline time is inverted to source seconds via the SAME
out_to_source the renderer uses (so the rendered timing is unchanged for the
current clip layout). Groups that span a source boundary are split so a group
never crosses sources. Backs up the raw project.json to project.json.capbak.

Usage: python scripts/migrate_caps_source.py <project_dir> [<project_dir> ...]
       python scripts/migrate_caps_source.py --all   # every project under projects/
"""
import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "render"))
from common import load_project, out_to_source  # noqa: E402


def migrate_project(pdir: Path):
    raw = json.loads((pdir / "project.json").read_text())
    caps = raw.get("captions") or []
    if not caps or all(c.get("capAnchor") == "source" for c in caps):
        return 0, "already source-anchored / no captions"
    p = load_project(pdir / "project.json")
    new_caps = []
    for cap in caps:
        if cap.get("capAnchor") == "source":
            new_caps.append(cap)
            continue
        base = {k: v for k, v in cap.items() if k != "words"}
        runs = []  # [(src, [(w, s0, s1), ...])]
        for w in cap.get("words", []):
            t0, t1 = w["t"], w["t"] + w["d"]
            ci, s0 = out_to_source(p, t0)
            if ci is None:
                continue
            cj, s1 = out_to_source(p, t1)
            clip = p["clips"][ci]
            src = clip.get("src", "main")
            if cj != ci:
                s1 = clip["out"]
            item = (w["w"], round(s0, 2), round(max(s1, s0 + 0.05), 2))
            if runs and runs[-1][0] == src:
                runs[-1][1].append(item)
            else:
                runs.append((src, [item]))
        for k, (src, ws) in enumerate(runs):
            g = dict(base)
            g["id"] = cap["id"] if k == 0 else "g" + uuid.uuid4().hex[:7]
            g["capAnchor"] = "source"
            g["src"] = src
            g["words"] = [{"w": w, "t0": a, "t1": b} for w, a, b in ws]
            new_caps.append(g)
    raw["captions"] = new_caps
    shutil.copyfile(pdir / "project.json", pdir / "project.json.capbak")
    (pdir / "project.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return len(new_caps), f"{len(caps)} → {len(new_caps)} groups"


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--all":
        base = Path(__file__).parent.parent / "projects"
        dirs = [d for d in base.iterdir() if (d / "project.json").is_file()]
    else:
        dirs = [Path(a) for a in args]
    for d in dirs:
        try:
            n, msg = migrate_project(d)
            print(f"{d.name}: {msg}")
        except Exception as e:  # noqa: BLE001
            print(f"{d.name}: ERROR {e}")


if __name__ == "__main__":
    main()
