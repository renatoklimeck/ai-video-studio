"""VISION track for take selection (REN-135) — this is what stops the AI from
being blind.

Loudness alone can't tell a real take from a rehearsal that happened to be
spoken loudly. What actually separates them is what the CAMERA sees: whether
he is facing the lens or reading off-screen, how steady he is, whether the
face is even framed.

YuNet gives a face box plus five landmarks (both eyes, nose, both mouth
corners). From those, per sampled frame:

  * facing   — how centred the nose is between the eyes (a head turned away or
               tilted down moves the nose off that midpoint). 1.0 = straight
               into the lens.
  * looking_down — nose sitting low relative to the eye line: reading a script.
  * size / pos   — framing, so a take shot at a different distance is visible.
  * motion       — frame-to-frame face movement; a settled take is steady.

Everything here is measurement, not judgement: the numbers go into the take
table and the model chooses.

Usage: python vision.py <project_dir> <media_rel> [--out cache/visiontrack.json]
"""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import cv2

# OpenCV's own thread pool deadlocks when its DNN is driven from a Python worker
# thread — the edit job hung here for 32 minutes at 0.6% CPU, holding the proxy
# open and never returning. One thread is also plenty at this sample rate.
cv2.setNumThreads(1)

sys.path.insert(0, str(Path(__file__).parent))
from renderer import resolve  # noqa: E402

MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 2.5
DETECT_W = 416.0
# Vision is an ENHANCEMENT to take selection, never a requirement. It gets a
# wall-clock budget and a sample ceiling so a slow or misbehaving decoder can
# only ever cost the edit this much — it can no longer stall it outright.
TIME_BUDGET = 60.0
MAX_SAMPLES = 1500


def analyse(src, sample_fps=SAMPLE_FPS, budget=TIME_BUDGET):
    """[{t, x, y, w, h, facing, down, ...}] sampled across the media.
    Returns whatever it managed within `budget` seconds — partial vision is
    still useful, a hung edit is not."""
    t_start = time.monotonic()
    if not MODEL.exists():
        return {"found": False, "samples": [], "reason": "yunet model missing"}
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / sample_fps)))
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.6)

    samples, i, prev = [], 0, None
    while True:
        # grab() advances without decoding; only the sampled frames are decoded.
        # At step=12 that is 12x less work than read()ing every 4K frame.
        if not cap.grab():
            break
        if i % step == 0:
            if len(samples) >= MAX_SAMPLES or time.monotonic() - t_start > budget:
                break
            ok, frame = cap.retrieve()
            if not ok:
                break
            H, W = frame.shape[:2]
            sc = DETECT_W / W
            small = cv2.resize(frame, (int(DETECT_W), int(H * sc)))
            det.setInputSize((small.shape[1], small.shape[0]))
            _, faces = det.detect(small)
            t = round(i / fps, 3)
            if faces is not None and len(faces):
                f = max(faces, key=lambda r: r[2] * r[3])
                x, y, w, h = (float(v) / sc for v in f[:4])
                # landmarks: rEye, lEye, nose, rMouth, lMouth (x,y pairs)
                lm = [(float(f[4 + k * 2]) / sc, float(f[5 + k * 2]) / sc) for k in range(5)]
                (rex, rey), (lex, ley), (nx, ny) = lm[0], lm[1], lm[2]
                eye_mid_x, eye_mid_y = (rex + lex) / 2, (rey + ley) / 2
                eye_dist = max(1e-3, math.hypot(lex - rex, ley - rey))
                # horizontal: nose off the eye midpoint => head turned away
                yaw = abs(nx - eye_mid_x) / eye_dist
                # vertical: nose far BELOW the eye line (normalised by eye span)
                # => chin down / reading something below the lens
                drop = (ny - eye_mid_y) / eye_dist
                facing = max(0.0, 1.0 - yaw * 2.0)
                mouth_mid_y = (lm[3][1] + lm[4][1]) / 2
                # mouth-to-eye span shrinks as the head pitches down
                pitch = (mouth_mid_y - eye_mid_y) / eye_dist
                motion = 0.0
                if prev is not None:
                    motion = math.hypot((x + w / 2) - prev[0], (y + h / 2) - prev[1]) / max(1.0, w)
                prev = (x + w / 2, y + h / 2)
                samples.append({
                    "t": t,
                    "x": round((x + w / 2) / W, 4), "y": round((y + h / 2) / H, 4),
                    "w": round(w / W, 4), "h": round(h / H, 4),
                    "facing": round(facing, 3), "drop": round(drop, 3),
                    "pitch": round(pitch, 3), "motion": round(motion, 4),
                })
            else:
                samples.append({"t": t, "face": False})
                prev = None
        i += 1
    cap.release()
    return {"found": any("facing" in s for s in samples),
            "fps": sample_fps, "samples": samples}


def summarise(track, t0, t1):
    """Aggregate the vision track over one take's time range."""
    ss = [s for s in track.get("samples", []) if t0 <= s["t"] <= t1]
    if not ss:
        return {}
    with_face = [s for s in ss if "facing" in s]
    if not with_face:
        return {"face_pct": 0.0}

    def med(key):
        v = sorted(s[key] for s in with_face)
        return round(v[len(v) // 2], 3)
    return {
        "face_pct": round(len(with_face) / len(ss), 2),
        "facing": med("facing"),          # 1.0 = straight to camera
        "pitch": med("pitch"),            # mouth-eye span; smaller = head down
        "face_h": med("h"),               # framing
        "motion": med("motion"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("media")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    pdir = Path(args.project_dir)
    src = resolve(pdir, args.media)
    if not src or not Path(src).is_file():
        sys.exit(f"source not found: {args.media}")
    track = analyse(src)
    out = Path(args.out) if args.out else (
        pdir / "cache" / f"visiontrack_{hashlib.sha1(args.media.encode()).hexdigest()[:10]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(track))
    n = sum(1 for s in track["samples"] if "facing" in s)
    print(f"done: {out} ({n}/{len(track['samples'])} frames with a face)")


if __name__ == "__main__":
    main()
