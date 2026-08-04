"""REN-125 — split the audio fade off the video fade, without losing what exists.

`clips[].fadeIn/fadeOut` and `overlays[].fadeIn/fadeOut` used to do BOTH jobs:
fade the picture to black AND ease the sound. Now `fadeIn/fadeOut` is the
picture and `aFadeIn/aFadeOut` is the sound.

A project written before the split therefore has audio fades hidden inside its
video fades. Copying the value across preserves exactly what those projects
sound like today; leaving them alone would silently drop every audio fade he
already set.

Idempotent — a clip that already carries aFadeIn is skipped.

    python scripts/migrate_afade.py               # every project
    python scripts/migrate_afade.py <project_dir> # just one
    python scripts/migrate_afade.py --dry-run
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRY = "--dry-run" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
dirs = [Path(a) for a in args] or sorted(d for d in (ROOT / "projects").iterdir() if d.is_dir())

touched = 0
for d in dirs:
    f = d / "project.json"
    if not f.exists():
        continue
    try:
        p = json.loads(f.read_text())
    except (OSError, ValueError):
        print(f"  ! {d.name}: unreadable, skipped")
        continue
    n = 0
    for key in ("clips", "overlays"):
        for it in p.get(key) or []:
            if "aFadeIn" in it or "aFadeOut" in it:
                continue                       # already migrated
            fi, fo = it.get("fadeIn") or 0, it.get("fadeOut") or 0
            if not fi and not fo:
                continue                       # nothing to carry over
            it["aFadeIn"], it["aFadeOut"] = fi, fo
            n += 1
    if not n:
        continue
    touched += 1
    print(f"  {'would update' if DRY else 'updated'} {d.name}: {n} item(s)")
    if not DRY:
        f.write_text(json.dumps(p, ensure_ascii=False, indent=1))

print(f"{touched} project(s) {'would change' if DRY else 'changed'}")
