"""Face retouch export pipeline — skin-only, texture-preserving, no whitening.

Design contract (design/README.md §Face Retouch): face-detection based,
smoothing applies to SKIN only while brows/lashes/hair stay crisp,
non-destructive (driven entirely by the clip's `rt` dict), full-res on export.
The web preview approximates this with a CSS filter; the divergence is
accepted and documented in the QA issue.

Slider -> operation mapping (all strengths scaled by the master m below):
  intensity : master scale m = 0.4 + 0.6*(intensity/100)  (mirrors the CSS
              preview formula's (0.4+0.6k) factor)
  smooth    : frequency separation — bilateral-filtered low band replaces the
              skin low band while the high band (pores, texture) is kept at
              >=70%, so skin evens out without going plastic
  even      : LAB a/b low-frequency variation pulled toward the skin mean
              (evens redness/blotches without changing luminance = no whitening)
  blem      : median-filtered plate blended in only where a pixel is a small
              dark outlier vs its neighborhood (spots, blemishes)
  shine     : specular highlights (V above the skin's 85th percentile)
              compressed toward that percentile
  plump     : subtle soft-glow (lighten-only blend of a gaussian layer) for a
              fuller, hydrated look
  eyes      : gentle brightness lift inside the eye regions
  circles   : under-eye band pulled toward the local skin plate (median) to
              soften dark circles

Face tracking: YuNet (cv2.FaceDetectorYN, render/models/*.onnx) every
DETECT_EVERY frames on a downscaled frame, EMA-smoothed box + landmarks
(eyes, mouth), kept alive for up to ~1.5s of missed detections. The skin mask
excludes the eye/mouth landmark regions and high-gradient areas (edge map), so
brows, lashes, lips and hair edges are never smoothed. The mask is feathered
and temporally smoothed (EMA) to avoid shimmer.
"""
from pathlib import Path

import cv2
import numpy as np

DETECT_EVERY = 6
DETECT_W = 480.0
MISS_KEEP = 8  # detection attempts (every DETECT_EVERY frames) ≈ 1.6s at 30fps
MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"


class Retouch:
    def __init__(self, rt):
        self.rt = rt
        self.det = None
        if MODEL.exists():
            self.det = cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.6)
        else:
            print(f"retouch: model missing ({MODEL}) — retouch disabled", flush=True)
        self.face = None         # EMA [x, y, w, h, rex, rey, lex, ley, mx, my]
        self.miss = MISS_KEEP + 1
        self.frame_i = -1
        self.prev_mask = None

    # ---------- detection ----------

    def _detect(self, frame):
        sc = DETECT_W / frame.shape[1]
        small = cv2.resize(frame, (int(DETECT_W), int(frame.shape[0] * sc)))
        self.det.setInputSize((small.shape[1], small.shape[0]))
        _, faces = self.det.detect(small)
        if faces is None or not len(faces):
            self.miss += 1
            return
        f = max(faces, key=lambda r: r[2] * r[3])
        # x, y, w, h, right eye, left eye, nose, right/left mouth corner, score
        mx, my = (f[10] + f[12]) / 2, (f[11] + f[13]) / 2
        vec = np.array([f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], mx, my],
                       dtype=np.float32) / sc
        if self.face is None or self.miss > MISS_KEEP:
            self.face = vec
        else:
            self.face = 0.7 * self.face + 0.3 * vec  # EMA smoothing = tracking
        self.miss = 0

    def _roi(self, W, H):
        """Expanded face crop (forehead + chin + margin), clamped to frame."""
        x, y, w, h = self.face[:4]
        cx, cy = x + w / 2, y + h / 2
        rw, rh = w * 1.6, h * 1.9
        x0 = int(max(0, cx - rw / 2)); y0 = int(max(0, cy - rh / 2))
        x1 = int(min(W, cx + rw / 2)); y1 = int(min(H, cy + rh / 2))
        return x0, y0, x1, y1

    def _masks(self, roi, fx, fy, fw, fh, eyes, mouth):
        """(skin, eye, under-eye) float masks [0..1] at roi resolution."""
        h, w = roi.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        cv2.ellipse(mask, (int(fx + fw / 2), int(fy + fh * 0.50)),
                    (int(fw * 0.60), int(fh * 0.72)), 0, 0, 360, 255, -1)

        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 178, 130))
        skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        mask = cv2.bitwise_and(mask, skin)

        er = max(4, int(fw * 0.14))
        eye_m = np.zeros((h, w), np.float32)
        for (ex, ey) in eyes:
            cv2.ellipse(mask, (int(ex), int(ey)), (er, int(er * 0.6)), 0, 0, 360, 0, -1)
            cv2.ellipse(eye_m, (int(ex), int(ey)), (int(er * 0.85), int(er * 0.5)), 0, 0, 360, 1.0, -1)
        under_m = np.zeros((h, w), np.float32)
        for (ex, ey) in eyes:
            cv2.ellipse(under_m, (int(ex), int(ey + er * 0.95)), (int(er * 0.95), int(er * 0.55)), 0, 0, 360, 1.0, -1)
        if mouth is not None:
            cv2.ellipse(mask, (int(mouth[0]), int(mouth[1])),
                        (int(fw * 0.24), int(fh * 0.11)), 0, 0, 360, 0, -1)

        # protect high-detail edges (brows, lashes, hairline, glasses)
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        edges = (mag > 110).astype(np.uint8) * 255
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
        mask[edges > 0] = 0

        k = max(3, int(fw * 0.06)) | 1
        skin_f = cv2.GaussianBlur(mask, (k, k), 0).astype(np.float32) / 255.0
        ke = max(3, int(fw * 0.05)) | 1
        eye_m = cv2.GaussianBlur(eye_m, (ke, ke), 0)
        under_m = cv2.GaussianBlur(under_m, (ke, ke), 0)
        return skin_f, eye_m, under_m

    # ---------- main ----------

    def apply(self, frame):
        """Retouch one BGR frame; returns the processed frame."""
        if self.det is None:
            return frame
        rt = self.rt
        self.frame_i += 1
        if self.frame_i % DETECT_EVERY == 0:
            self._detect(frame)
        if self.face is None or self.miss > MISS_KEEP:
            self.prev_mask = None
            return frame

        H, W = frame.shape[:2]
        x0, y0, x1, y1 = self._roi(W, H)
        if x1 - x0 < 24 or y1 - y0 < 24:
            return frame
        roi = frame[y0:y1, x0:x1]
        fx, fy, fw, fh = self.face[0] - x0, self.face[1] - y0, self.face[2], self.face[3]
        eyes = [(self.face[4] - x0, self.face[5] - y0), (self.face[6] - x0, self.face[7] - y0)]
        mouth = (self.face[8] - x0, self.face[9] - y0)

        mask, eye_m, under_m = self._masks(roi, fx, fy, fw, fh, eyes, mouth)
        if self.prev_mask is not None and self.prev_mask.shape == mask.shape:
            mask = 0.55 * mask + 0.45 * self.prev_mask  # temporal stability
        self.prev_mask = mask
        m3 = mask[:, :, None]
        under_m = under_m * mask

        m = 0.4 + 0.6 * (rt.get("intensity", 35) / 100.0)  # master scale
        s = {k: rt.get(k, 0) / 100.0 * m for k in
             ("smooth", "even", "blem", "shine", "plump", "eyes", "circles")}
        src = roi.astype(np.float32)
        out = src.copy()
        sigma = max(2.0, fw / 90.0)

        if s["smooth"] > 0.01:
            low = cv2.GaussianBlur(src, (0, 0), sigma * 1.6)
            high = src - low
            # The frequency separation used to cancel itself out. A bilateral with
            # a fixed ~9px radius, followed by a gaussian of sigma*1.6 — 9.87px on
            # a 555px face — is a blur WIDER than the filter it is meant to
            # preserve, so it erased the bilateral's work: measured, the surviving
            # low-band correction was 0.113/255. All "smooth" really did was take
            # 30% off the high band, and the whole effect at maximum was 2% of the
            # face. He reported the feature as doing nothing, and he was nearly
            # right.
            #
            # Fix: run the bilateral on a face-NORMALISED copy, so its radius means
            # the same thing whatever the resolution, and let it take more texture.
            # Measured on a real frame: 1.41 → 4.13 MAD inside the face box, for
            # 62ms/frame instead of 40ms. (Doing it at full res with a proportional
            # sigmaSpace gives no more effect and costs 1.2s/frame.)
            small_w = 200
            scale = small_w / max(1.0, fw)
            if scale < 1.0:
                tiny = cv2.resize(roi, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                tiny = cv2.bilateralFilter(tiny, 9, 30 + 50 * s["smooth"], 7)
                base = cv2.resize(tiny, (roi.shape[1], roi.shape[0]),
                                  interpolation=cv2.INTER_LINEAR).astype(np.float32)
            else:
                base = cv2.bilateralFilter(roi, 9, 30 + 50 * s["smooth"], 7).astype(np.float32)
            base_low = cv2.GaussianBlur(base, (0, 0), sigma * 1.6)
            keep = 1.0 - 0.6 * s["smooth"]  # texture stays >= 40% at the maximum
            sm = base_low + high * keep
            out = out * (1 - s["smooth"]) + sm * s["smooth"]

        if s["even"] > 0.01:
            lab = cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)
            for ch in (1, 2):
                lowc = cv2.GaussianBlur(lab[:, :, ch], (0, 0), sigma * 3)
                mean = (lowc * mask).sum() / max(1.0, mask.sum())
                lab[:, :, ch] += (mean - lowc) * (0.6 * s["even"]) * mask
            out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float32)

        if s["blem"] > 0.01:
            k = max(3, int(fw / 40)) | 1
            med = cv2.medianBlur(np.clip(out, 0, 255).astype(np.uint8), k).astype(np.float32)
            g_out = out.mean(axis=2)
            g_med = med.mean(axis=2)
            spot = np.clip((g_med - g_out - 6) / 24.0, 0, 1)[:, :, None]  # dark outliers
            out = out + (med - out) * spot * s["blem"]

        if s["shine"] > 0.01 and (mask > 0.5).any():
            hsv = cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
            v = hsv[:, :, 2]
            p85 = np.percentile(v[mask > 0.5], 85)
            over = np.clip(v - p85, 0, None)
            hsv[:, :, 2] = v - over * 0.6 * s["shine"] * mask
            out = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)

        if s["plump"] > 0.01:
            glow = cv2.GaussianBlur(out, (0, 0), sigma * 2.5)
            out = out + (np.maximum(glow, out) - out) * 0.35 * s["plump"]

        if s["eyes"] > 0.01:
            out = out + (255 - out) * 0.12 * s["eyes"] * eye_m[:, :, None]

        if s["circles"] > 0.01:
            plate = cv2.medianBlur(np.clip(out, 0, 255).astype(np.uint8),
                                   max(5, int(fw / 24)) | 1).astype(np.float32)
            lift = np.clip(plate * 1.06 + 6, 0, 255)
            out = out + (lift - out) * 0.6 * s["circles"] * under_m[:, :, None]

        # blend through the skin mask, PLUS the eye regions when eye brightening
        # is on (eyes are excluded from the skin mask, so without this the eyes
        # op would be zeroed by the final blend)
        blend_m = m3
        if s["eyes"] > 0.01:
            blend_m = np.clip(mask + eye_m, 0, 1)[:, :, None]
        blended = src * (1 - blend_m) + out * blend_m
        frame[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)
        return frame
