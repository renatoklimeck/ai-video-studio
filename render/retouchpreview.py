"""Face-retouch PREVIEW for one clip (REN-84): renders a proxy-res clip of just
that clip through the REAL retouch pipeline (render/retouch.py), including the
clip's own audio, so the web player can swap in an accurate preview — the same
two-layer pattern as the background-removal preview (render/segbg.py).

Usage: python retouchpreview.py <project_dir> <clip_id>
Writes projects/<id>/cache/retouch_<clipId>.mp4 and prints its relative path.
The client marks rt.processed/stale and stores rt._cache on job completion.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import hex_rgb, load_project
from renderer import FFMPEG, BgRemover, cover_box, resolve
from retouch import Retouch

PROXY_W = 540


def main():
    pdir = Path(sys.argv[1])
    clip_id = sys.argv[2]
    project = load_project(pdir / "project.json")
    clip = next(c for c in project["clips"] if c["id"] == clip_id)
    rt = clip.get("rt") or {}
    src = project["sources"].get(clip.get("src", "main")) or {}

    W = int(PROXY_W * project["w"] / max(project["w"], project["h"])) // 2 * 2
    H = int(PROXY_W * project["h"] / max(project["w"], project["h"])) // 2 * 2
    fps = float(project.get("fps") or 30)
    main_path = resolve(pdir, src.get("path"))

    cache = pdir / "cache"
    cache.mkdir(exist_ok=True)
    out = cache / f"retouch_{clip_id}.mp4"

    retoucher = Retouch(rt)
    # a clip can have BOTH background removal and retouch; the export applies bg
    # THEN retouch (renderer.py), so the preview must composite both or it would
    # diverge. Mirror that order here when the clip has bg.
    bg = clip.get("bg")
    remover = BgRemover() if bg else None
    back = None
    if bg:
        back = np.zeros((H, W, 3), np.uint8)
        back[:] = hex_rgb(bg.get("color", "#000000"))[::-1]
        if bg.get("image"):
            im = cv2.imread(resolve(pdir, bg["image"]))
            if im is not None:
                back = cover_box(im, W, H)

    cap = cv2.VideoCapture(main_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, clip["in"] * 1000.0)
    n = int((clip["out"] - clip["in"]) * fps)

    tmp = tempfile.mkdtemp(prefix="retouch_")
    vtmp = str(Path(tmp) / "v.mkv")
    ff = subprocess.Popen([
        FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
        "-r", f"{fps:.6f}", "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "22", "-g", "15", "-x264-params", "scenecut=0",
        "-pix_fmt", "yuv420p", "-bf", "0", "-f", "matroska", vtmp,
    ], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for i in range(n):
        ok, frame = cap.read()
        if not ok:
            break
        frame = cover_box(frame, W, H)
        if remover is not None:
            m = remover.mask(frame, W, H, max(2, bg.get("feather", 4) * W / project["w"]))
            frame = (frame.astype(np.float32) * m + back.astype(np.float32) * (1 - m)).astype(np.uint8)
        frame = retoucher.apply(frame)
        ff.stdin.write(frame.tobytes())
        if i % 15 == 0:
            print(f"PROGRESS {i}/{n}", flush=True)
    cap.release()
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("video encode failed")

    # -map 1:a? keeps audio optional — silent sources (screen caps, b-roll) must
    # not fail the whole job and leave the client pointing at a missing file
    r = subprocess.run([
        FFMPEG, "-y", "-i", vtmp, "-ss", f"{clip['in']:.4f}", "-to", f"{clip['out']:.4f}",
        "-i", main_path, "-map", "0:v", "-map", "1:a?", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        "-shortest", str(out),
    ], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        sys.exit(f"mux failed: {r.stderr[-400:]}")
    print(f"PROGRESS {n}/{n}", flush=True)
    print(f"done: cache/retouch_{clip_id}.mp4", flush=True)


if __name__ == "__main__":
    main()
