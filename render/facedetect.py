"""Face track for a source video (REN-84): sample YuNet across the media and
write a normalized bbox track, so the app can (a) show an honest
"face detected / no face" chip per clip and (b) centre Auto zoom's punch-in on
the face instead of on the middle of the frame.

The client passes the 720p PROXY when available (fast keyframe seeks, GOP 15),
so we sample by SEEKING to each timestamp instead of decoding every frame —
O(samples) not O(frames), fast even on long sources.

Usage: python facedetect.py <project_dir> <media_path>
Writes projects/<id>/cache/facetrack_<sha1(path)>.json:
  {"found": bool, "fps": <sample fps>, "dur": <seconds>,
   "samples": [{"t": srcSeconds, "x": cx0..1, "y": cy0..1, "w": 0..1, "h": 0..1}]}
x/y are the face-box CENTER as fractions of frame w/h; w/h are size fractions.
Prints "PROGRESS i/n" and "done: <rel path>".
"""
import hashlib
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from renderer import resolve

MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 3.0        # detections per second of source (face moves slowly)
DETECT_W = 416.0


def main():
    pdir = Path(sys.argv[1])
    src_path = sys.argv[2]
    src = resolve(pdir, src_path)
    if not src or not Path(src).is_file():
        sys.exit(f"source not found: {src_path}")

    cache = pdir / "cache"
    cache.mkdir(exist_ok=True)
    out = cache / f"facetrack_{hashlib.sha1(src_path.encode()).hexdigest()[:10]}.json"

    if not MODEL.exists():
        out.write_text(json.dumps({"found": False, "samples": [], "reason": "model missing"}))
        print(f"done: cache/{out.name}", flush=True)
        return

    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = (total / fps) if fps and total else 1.0
    # sequential decode is faster than per-sample seeking on the 720p proxy;
    # only run YuNet every `step` frames to hit the target sample rate
    step = max(1, int(round(fps / SAMPLE_FPS)))
    n = max(1, (total or int(dur * fps)) // step)

    det = cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.6)
    samples, found, i, done = [], False, 0, 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            H, W = frame.shape[:2]
            sc = DETECT_W / W
            small = cv2.resize(frame, (int(DETECT_W), int(H * sc)))
            det.setInputSize((small.shape[1], small.shape[0]))
            _, faces = det.detect(small)
            if faces is not None and len(faces):
                f = max(faces, key=lambda r: r[2] * r[3])
                x, y, w, h = float(f[0]) / sc, float(f[1]) / sc, float(f[2]) / sc, float(f[3]) / sc
                samples.append({
                    "t": round(i / fps, 3),
                    "x": round((x + w / 2) / W, 4), "y": round((y + h / 2) / H, 4),
                    "w": round(w / W, 4), "h": round(h / H, 4),
                })
                found = True
            done += 1
            if done % 20 == 0:
                print(f"PROGRESS {done}/{n}", flush=True)
        i += 1
    cap.release()

    out.write_text(json.dumps({"found": found, "fps": SAMPLE_FPS,
                               "dur": round(dur, 2), "samples": samples}))
    print(f"PROGRESS {n}/{n}", flush=True)
    print(f"done: cache/{out.name}", flush=True)


if __name__ == "__main__":
    main()
