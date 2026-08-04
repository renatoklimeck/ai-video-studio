"""Face retouch export pipeline — skin-only, texture-preserving, no whitening.

Design contract (design/README.md §Face Retouch): face-detection based,
smoothing applies to SKIN only while brows/lashes/hair stay crisp,
non-destructive (driven entirely by the clip's `rt` dict), full-res on export.
The web preview approximates this with a CSS filter; the divergence is
accepted and documented in the QA issue.

Slider -> operation mapping (all strengths scaled by the master m below):
  intensity : master scale m = 0.4 + 0.6*(intensity/100)  (mirrors the CSS
              preview formula's (0.4+0.6k) factor)
  smooth    : frequency separation, then the edge-preserving filter runs on the
              LOW band only — blotches merge, pores are added back untouched
              (>=88%). The direction matters: the old chain did it the other way
              round and measured 74% texture with 95% of the blotchiness still
              there, which is the recipe for plastic skin (REN-170)
  even      : LAB a/b low-frequency variation pulled toward the skin mean
              (evens redness/blotches without changing luminance = no
              whitening). Fades out where the light is extreme — a specular
              highlight has no colour of its own and painting skin chroma onto
              it prints an orange patch
  blem      : median plate built on a face normalised to 300px (so the kernel
              means the same at preview and export res), blended in only where
              a pixel is a dark OR red outlier vs its neighbourhood
  dewrinkle : dark lines in the band between pores and shading (difference of
              two gaussians, dark side only) lifted toward the surrounding skin
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

# A dark line this deep (levels of 255) is structure, not a crease — a beard
# edge, a nostril, the lash line. The dewrinkle lift fades to nothing by here,
# which is what keeps the face from going waxy at the top of the slider.
DEEP_DB = 26.0

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

        # The generic skin range is a colour range, and a beige wall sits inside
        # it. That let the ellipse's corner catch a patch of background, where
        # "even tone" then pulled the wall toward his skin colour and left a red
        # smudge next to his eyebrow (REN-170). Judge against THIS face instead:
        # anything far from the median colour of what is already masked is not
        # his skin, whatever the generic range says.
        sel = mask > 0
        if sel.sum() > 500:
            med = np.median(ycrcb[sel].reshape(-1, 3), axis=0)
            d = np.abs(ycrcb.astype(np.float32) - med)
            near = ((d[:, :, 1] < 12) & (d[:, :, 2] < 12)).astype(np.uint8) * 255
            mask = cv2.bitwise_and(mask, near)

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
             ("smooth", "even", "blem", "shine", "plump", "eyes", "circles", "dewrinkle")}
        src = roi.astype(np.float32)
        out = src.copy()
        sigma = max(2.0, fw / 90.0)

        if s["smooth"] > 0.01:
            # REBUILT (REN-170). The old chain blurred the wrong band. Measured
            # on his own face at maximum: the texture inside the skin fell to
            # 74% of the original while the blotchiness stayed at 95% — it took
            # the pores and left the uneven patches, which is the exact recipe
            # for "embasado e ruim". What reads as good skin is the opposite:
            # blotches merge, pores stay.
            #
            # So the separation happens FIRST, and the edge-preserving filter is
            # applied to the LOW band only. The high band — pores, beard,
            # lashes — is added back essentially untouched.
            # The whole low band is computed SMALL. It is smooth by definition,
            # so a face normalised to 256px carries all of it, and that turns a
            # full-resolution blur plus three full-resolution bilateral passes
            # (309 ms/frame, too slow to feel live) into the same work on a
            # thumbnail. Only the subtraction and the recombination happen at
            # full resolution, where the texture lives.
            NW = 128.0
            sc = min(1.0, NW / max(1.0, fw))
            sig_sep = max(1.5, fw / 55.0)     # below this = blotch, above = pore
            u8 = np.clip(src, 0, 255).astype(np.uint8)
            small = (cv2.resize(u8, (0, 0), fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
                     if sc < 1.0 else u8)
            small = cv2.GaussianBlur(small, (0, 0), max(0.6, sig_sep * sc))
            up = lambda im: (cv2.resize(im, (roi.shape[1], roi.shape[0]),  # noqa: E731
                                        interpolation=cv2.INTER_LINEAR).astype(np.float32)
                             if sc < 1.0 else im.astype(np.float32))
            low = up(small)
            high = src - low
            # three gentle passes flatten far more than one strong one, and keep
            # the real shading (nose, jaw) that one strong pass would iron out
            # d is pinned rather than derived from sigmaSpace: letting OpenCV
            # size the kernel gave a ~29px radius and 294 ms/frame, and three
            # cheap passes reach further than one expensive one anyway (the
            # radii add in quadrature).
            sc_col = 18 + 80 * s["smooth"]
            for _ in range(3):
                small = cv2.bilateralFilter(small, 11, sc_col, 13)
            flat = up(small)
            keep = 1.0 - 0.12 * s["smooth"]   # texture never drops below 88%
            sm = flat + high * keep
            out = out * (1 - s["smooth"]) + sm * s["smooth"]

        if s["even"] > 0.01:
            lab = cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)
            # Hold off where the light is extreme. A specular highlight has
            # almost no colour of its own, so pulling it toward the skin's
            # average chroma paints saturated skin colour onto something that is
            # still bright — which is exactly the orange patch that appeared on
            # his forehead at maximum. Same for deep shadow. The correction
            # fades out as the luminance leaves the range the face actually
            # lives in (REN-170).
            Lm = float(np.median(lab[:, :, 0][mask > 0.5])) if (mask > 0.5).any() else 128.0
            trust = np.clip(1.0 - np.abs(lab[:, :, 0] - Lm) / 45.0, 0, 1) * mask
            for ch in (1, 2):
                lowc = cv2.GaussianBlur(lab[:, :, ch], (0, 0), sigma * 3)
                mean = (lowc * mask).sum() / max(1.0, mask.sum())
                # 0.9 and never more: this pulls each patch TOWARD the skin's
                # mean, so a gain of 1 lands exactly on it and anything above
                # overshoots — the blotch comes back inverted. At 1.15 that
                # printed an orange patch across his forehead at maximum.
                lab[:, :, ch] += (mean - lowc) * (0.9 * s["even"]) * trust
            out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float32)

        if s["blem"] > 0.01:
            # REBUILT (REN-160). The old version was measured at 0.05/255 on
            # export — nothing — and 0.96 at preview resolution, i.e. it did two
            # different things depending on where it ran. Both came from
            # `k = fw/40`: the median kernel has to be BIGGER than the spot it
            # removes, and fw/40 is 13px on a 555px face, smaller than a real
            # blemish; at preview res the same expression collapses to 3, where
            # a median is just a denoiser.
            #
            # So the plate is built on a face NORMALISED to a fixed width. A
            # kernel is then a fixed fraction of a face at every resolution, and
            # preview and export finally agree.
            NB = 300.0
            sc = min(1.0, NB / max(1.0, fw))
            cur = np.clip(out, 0, 255).astype(np.uint8)
            small = (cv2.resize(cur, (0, 0), fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
                     if sc < 1.0 else cur)
            if min(small.shape[:2]) >= 12:
                med_s = cv2.medianBlur(small, 11)     # ~4% of the face = a spot
                med = (cv2.resize(med_s, (cur.shape[1], cur.shape[0]),
                                  interpolation=cv2.INTER_LINEAR).astype(np.float32)
                       if sc < 1.0 else med_s.astype(np.float32))
                # a blemish is DARKER than the skin around it, REDDER, or both —
                # judging on luminance alone missed the red ones entirely
                dark = np.clip((med.mean(axis=2) - out.mean(axis=2) - 2.5) / 12.0, 0, 1)
                la = cv2.cvtColor(cur, cv2.COLOR_BGR2LAB)[:, :, 1].astype(np.float32)
                lm = cv2.cvtColor(np.clip(med, 0, 255).astype(np.uint8),
                                  cv2.COLOR_BGR2LAB)[:, :, 1].astype(np.float32)
                red = np.clip((la - lm - 1.5) / 6.0, 0, 1)
                spot = np.maximum(dark, red)[:, :, None]
                out = out + (med - out) * spot * s["blem"]

        if s["dewrinkle"] > 0.01:
            # Wrinkles are dark LINES at a scale between pores and shading: a
            # forehead line is far wider than a pore and far narrower than the
            # shadow under a brow. Isolate that band (difference of two
            # gaussians) and keep only its dark side — a line, never a highlight.
            #
            # DEPTH IS THE WHOLE TRICK (REN-170). Lifting every dark thing in
            # that band is what made the face go waxy: a beard edge and a
            # nostril live in the same band as a crease, and flattening them
            # flattens the face. A crease is a few levels deep; the structures
            # that must survive are tens. So the lift fades out with depth and
            # only ever touches the shallow end.
            sig_a = max(1.0, fw / 150.0)
            sig_b = max(3.0, fw / 45.0)
            band = cv2.GaussianBlur(out, (0, 0), sig_a) - cv2.GaussianBlur(out, (0, 0), sig_b)
            dark = np.minimum(band, 0)
            shallow = np.clip((DEEP_DB - np.abs(dark)) / DEEP_DB, 0, 1)
            out = out - dark * shallow * (1.5 * s["dewrinkle"])

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
