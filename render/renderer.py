"""Export engine: project.json (schema v2) -> final MP4.
Handles: multi-source timeline cuts, per-clip zoom keyframes, whole-frame fade
in/out to black, per-clip background removal (rembg) with replacement
image/color, image/video overlays
(rounded corners, cover crop center/top, reversible image cutout), karaoke +
static caption groups (timeline time, wrapped like the web preview), headline
text blocks, and a mixed audio bed (per-clip voice with fades + audio tracks +
overlay-video audio) with fades.

The web preview and this renderer are the same contract: everything drawn here
must match web/src math. Preview-only UI (vignette, chips) is NOT exported.

Speed (REN-139): the frame loop is split into contiguous chunks rendered by
worker processes (one `python renderer.py ... --worker` each, so every worker
owns its own cv2.VideoCapture handles), and each chunk is encoded on the Apple
Silicon media engine (h264_videotoolbox) instead of libx264. Segments are
stitched with the ffmpeg concat demuxer in the same pass that muxes the audio.
Chunk boundaries never fall inside a clip that carries frame-to-frame state
(the bg-removal EMA); when a chunk does start mid-clip the
worker replays the decode exactly like the serial renderer (seek to the clip
start, then grab forward) so the emitted frames are bit-identical.

Usage: python renderer.py <project_dir> <out.mp4> [--scale 0.5]
Prints "PROGRESS i/n" lines for the job runner.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from common import (REF_H, caption_bounds, draw_caption, draw_text_block, hex_rgb,
                    item_alpha, load_project, materialize_cap, out_duration,
                    out_to_source, pick_caption_at, clip_out_start, zoom_at)

FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
if not Path(FFMPEG).exists():
    FFMPEG = "ffmpeg"


def perf_cores():
    """Apple Silicon performance-core count (falls back to half the logical CPUs)."""
    try:
        r = subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                           capture_output=True, text=True, timeout=5)
        n = int(r.stdout.strip())
        if n > 0:
            return n
    except Exception:  # noqa: BLE001 — non-Apple / older kernels
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def resolve(project_dir, p):
    if not p:
        return None
    q = Path(p)
    return str(q if q.is_absolute() else Path(project_dir) / q)


class BgRemover:
    def __init__(self):
        from rembg import new_session, remove
        self._remove, self.sess = remove, new_session("u2netp")
        self.prev = None

    def mask(self, frame_bgr, W, H, feather):
        small = cv2.resize(frame_bgr, (540, int(540 * H / W)))
        m = np.array(self._remove(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)),
                                  session=self.sess, only_mask=True), dtype=np.uint8)
        if m.ndim == 3:
            m = m[:, :, 0]
        m = cv2.resize(m, (W, H))
        k = max(1, int(feather))
        m = cv2.GaussianBlur(m, (k * 2 + 1, k * 2 + 1), 0).astype(np.float32) / 255.0
        if self.prev is not None:
            m = 0.6 * m + 0.4 * self.prev
        self.prev = m
        return m[:, :, None]


def open_capture(path):
    """cv2.VideoCapture, with the ffmpeg decoder thread count capped when we are
    one of N render workers (default 8 threads x N processes thrashes an 8-core
    machine). Decoding is deterministic either way — same frames out."""
    n = int(os.environ.get("VSTUDIO_DEC_THREADS") or 0)
    if n > 0:
        try:
            cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, n])
            if cap.isOpened():
                return cap
        except Exception:  # noqa: BLE001 — old OpenCV without the params ctor
            pass
    return cv2.VideoCapture(path)


class OverlayVideo:
    def __init__(self, path, fps_out):
        self.cap = open_capture(path)
        self.fps = self.cap.get(5) or fps_out
        self.idx = -1
        self.frame = None

    def at(self, rel_t):
        want = int(rel_t * self.fps)
        if want < self.idx:  # seek back (scrub) — rare in export
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, want)
            self.idx = want - 1
        while self.idx < want:
            ok, fr = self.cap.read()
            if not ok:
                return self.frame
            self.frame = fr
            self.idx += 1
        return self.frame


def cover_box(frame, bw, bh, crop="center"):
    ih, iw = frame.shape[:2]
    sc = max(bw / iw, bh / ih)
    r = cv2.resize(frame, (max(1, int(round(iw * sc))), max(1, int(round(ih * sc)))))
    ox = max(0, (r.shape[1] - bw) // 2)
    oy = 0 if crop == "top" else max(0, (r.shape[0] - bh) // 2)
    return r[oy:oy + bh, ox:ox + bw]


_round_masks = {}


def rounded_mask(bw, bh, radius):
    """Float [0..1] rounded-rect alpha, mirrors CSS border-radius+overflow:hidden."""
    key = (bw, bh, radius)
    if key not in _round_masks:
        im = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(im).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=radius, fill=255)
        _round_masks[key] = np.array(im, dtype=np.float32)[:, :, None] / 255.0
    return _round_masks[key]


def build_audio_filter(project, project_dir, out_dur, tmpdir):
    """Returns (inputs list, filter_script_path). Voice = per-clip trim from the
    clip's own source, concatenated; then audio tracks + audible video overlays.
    Muted tracks (REN-112) are dropped from the mix: video muted → the main voice
    is silenced; audio/overlays muted → those inputs are skipped."""
    def tflag(tk, fl):
        return bool((project.get("tracks") or {}).get(tk, {}).get(fl))
    inputs, input_idx = [], {}

    def input_for(path):
        if path not in input_idx:
            input_idx[path] = len(inputs)
            inputs.append(path)
        return input_idx[path]

    parts, seg_labels = [], []
    for i, c in enumerate(project["clips"]):
        src = project["sources"].get(c.get("src", "main")) or {}
        idx = input_for(resolve(project_dir, src.get("path")))
        d = c["out"] - c["in"]
        # AUDIO-only micro fade at every cut (>= 8ms) so there are no clicks/pops
        # between clips; a real audio fade wins if larger.
        #
        # `aFadeIn/aFadeOut` is the AUDIO fade (REN-125); `fadeIn/fadeOut` fades
        # the picture to black. They used to be one number, so easing the sound
        # in also dipped the image — not what the dot on the timeline means, and
        # not what CapCut does. Old projects fall back to the video fade, which
        # is exactly what they had before.
        declick = min(0.008, d / 2)
        afi = c.get("aFadeIn", c.get("fadeIn")) or 0
        afo = c.get("aFadeOut", c.get("fadeOut")) or 0
        fin = min(max(afi, declick), d)
        fout = min(max(afo, declick), d)
        fades = f",afade=t=in:st=0:d={fin:.3f},afade=t=out:st={max(0, d - fout):.3f}:d={fout:.3f}"
        volf = f",volume={float(c.get('vol', 1.0)):.4f}" if float(c.get("vol", 1.0)) != 1.0 else ""
        parts.append(f"[{idx}:a]atrim=start={c['in']:.4f}:end={c['out']:.4f},asetpts=PTS-STARTPTS{volf}{fades}[sa{i}];")
        seg_labels.append(f"[sa{i}]")
    parts.append("".join(seg_labels) + f"concat=n={len(seg_labels)}:v=0:a=1[amain_raw];")
    # video track muted → keep the segment (timing) but silence the main voice
    parts.append("[amain_raw]" + ("volume=0" if tflag("video", "muted") else "anull") + "[amain];")

    mix = ["[amain]"]
    items = [] if tflag("audio", "muted") else [
        {"path": a.get("path"), "t0": a.get("t0", 0), "t1": a.get("t1"),
         "vol": a.get("vol", 1.0), "fadeIn": a.get("fadeIn", 0), "fadeOut": a.get("fadeOut", 0)}
        for a in project.get("audios", [])]
    for o in project.get("overlays", []):
        if not tflag("overlays", "muted") and o.get("kind") == "video" and float(o.get("vol", 0)) > 0:
            # same split as the clips (REN-125): the overlay's fadeIn/fadeOut is
            # its picture, aFadeIn/aFadeOut its sound
            items.append({"path": o.get("path"), "t0": o.get("t0", 0), "t1": o.get("t1"),
                          "vol": o["vol"],
                          "fadeIn": o.get("aFadeIn", o.get("fadeIn")) or 0,
                          "fadeOut": o.get("aFadeOut", o.get("fadeOut")) or 0})
    for j, a in enumerate(items):
        p = resolve(project_dir, a["path"])
        if not p or not Path(p).is_file():
            continue  # missing audio file: skip the track instead of breaking the mux
        idx = input_for(p)
        t0 = float(a.get("t0", 0))
        end = float(a["t1"]) if a.get("t1") else out_dur
        dur = max(0.1, end - t0)
        fades = ""
        if a.get("fadeIn"):
            fades += f",afade=t=in:st=0:d={a['fadeIn']:.3f}"
        if a.get("fadeOut"):
            fades += f",afade=t=out:st={max(0, dur - a['fadeOut']):.3f}:d={a['fadeOut']:.3f}"
        delay = int(t0 * 1000)
        parts.append(f"[{idx}:a]atrim=start=0:end={dur:.4f},asetpts=PTS-STARTPTS,"
                     f"volume={float(a.get('vol', 1.0)):.4f}{fades},adelay={delay}|{delay}[ax{j}];")
        mix.append(f"[ax{j}]")
    if len(mix) > 1:
        parts.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[aout]")
    else:
        parts.append("[amain]anull[aout]")
    fpath = Path(tmpdir) / "afilter.txt"
    fpath.write_text("".join(parts))
    return inputs, str(fpath)


# ---------- encoder selection (REN-139) ----------

def encoder_args(kind, W, H, fps, scale):
    """ffmpeg -c:v ... args. 'vt' = Apple Silicon media engine (seconds instead
    of minutes); 'x264' = the original software path, kept as the fallback for
    machines without VideoToolbox. VideoToolbox ignores -crf, so quality is
    pinned with a resolution-derived bitrate (~0.30 bits/pixel at full scale,
    which sits above the libx264 -crf 16 bitrate this project used to produce).
    -bf 0 (no B-frames) matches the old output and keeps segment concat exact."""
    bpp = 0.30 if scale >= 0.9 else 0.20
    kbps = max(2000, int(W * H * fps * bpp / 1000))
    if kind == "vt":
        return ["-c:v", "h264_videotoolbox", "-b:v", f"{kbps}k",
                "-maxrate", f"{int(kbps * 1.4)}k", "-bufsize", f"{kbps * 2}k",
                "-pix_fmt", "yuv420p", "-bf", "0"]
    # N parallel libx264 encoders must share the cores, or they thrash
    nth = int(os.environ.get("VSTUDIO_ENC_THREADS") or 0)
    return ["-c:v", "libx264", "-preset", "slow" if scale >= 0.9 else "medium",
            "-crf", "16" if scale >= 0.9 else "19", "-pix_fmt", "yuv420p", "-bf", "0"] \
        + (["-threads", str(nth)] if nth > 0 else [])


def pick_encoder(W, H, fps, scale):
    """'vt' when h264_videotoolbox actually opens at this size, else 'x264'."""
    if os.environ.get("VSTUDIO_ENCODER") in ("vt", "x264"):
        return os.environ["VSTUDIO_ENCODER"]
    try:
        r = subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
             "-i", f"color=c=black:s={W}x{H}:r={fps:.6f}", "-frames:v", "2",
             *encoder_args("vt", W, H, fps, scale), "-f", "null", "-"],
            capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            return "vt"
        print(f"h264_videotoolbox unavailable — falling back to libx264 "
              f"({(r.stderr or '').strip().splitlines()[-1:] or ['?']})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"h264_videotoolbox probe failed ({e}) — falling back to libx264", flush=True)
    return "x264"


# ---------- chunk planning (REN-139) ----------

def clip_first_frame(project, ci, fps, at_or_after):
    """First output frame index that belongs to clip `ci`, walking back from a
    frame known to be inside it. Uses out_to_source so it agrees exactly with
    the render loop's own clip mapping."""
    fi = at_or_after
    while fi > 0 and out_to_source(project, (fi - 1) / fps)[0] == ci:
        fi -= 1
    return fi


def clip_boundary_frames(project, n_frames, fps):
    """Output frame index where each clip starts (excluding 0). Estimated from
    the cumulative durations, then walked onto the exact frame with out_to_source
    so it always agrees with the render loop's own clip mapping."""
    bounds, acc = [], 0.0
    for i, c in enumerate(project["clips"][:-1]):
        acc += c["out"] - c["in"]
        fi = min(max(int(acc * fps), 0), n_frames)
        while fi > 0 and out_to_source(project, (fi - 1) / fps)[0] >= i + 1:
            fi -= 1
        while fi < n_frames and out_to_source(project, fi / fps)[0] < i + 1:
            fi += 1
        if 0 < fi < n_frames:
            bounds.append(fi)
    return sorted(set(bounds))


def plan_chunks(project, n_frames, fps, n_workers):
    """Contiguous [start, end) frame ranges, one per worker. Cuts land on clip
    boundaries when one is close by (zero decode replay); otherwise they land
    exactly on the balanced position, which is only allowed inside clips with
    no frame-to-frame state (the bg-removal EMA)."""
    if n_workers <= 1 or n_frames < 2 * n_workers:
        return [(0, n_frames)]
    bounds = clip_boundary_frames(project, n_frames, fps)
    stateful = {i for i, c in enumerate(project["clips"]) if c.get("bg")}
    step = n_frames / n_workers
    cuts = set()
    for k in range(1, n_workers):
        ideal = int(round(k * step))
        near = min(bounds, key=lambda b: abs(b - ideal)) if bounds else None
        if near is not None and abs(near - ideal) <= 0.25 * step:
            cuts.add(near)
        elif out_to_source(project, min(ideal, n_frames - 1) / fps)[0] not in stateful \
                and 0 < ideal < n_frames:
            cuts.add(ideal)
        elif near is not None:
            cuts.add(near)
    pts = sorted({0, n_frames} | cuts)
    return [(a, b) for a, b in zip(pts, pts[1:]) if b > a]


# ---------- frame rendering ----------

_MISS = object()  # cache sentinel: "not computed yet" (None means "nothing drawn")


def render_range(project, pdir, args, W, H, fps, out_dur, n_frames, f0, f1, out_path,
                 enc_kind, quiet=False):
    """Render output frames [f0, f1) and encode them to out_path (matroska).
    Verbatim the original serial loop; only the range and the initial decode
    replay are new."""
    def tflag(tk, fl):
        return bool((project.get("tracks") or {}).get(tk, {}).get(fl))
    video_hidden = tflag("video", "hidden")
    caps_hidden = tflag("captions", "hidden")
    texts_hidden = tflag("texts", "hidden")
    ov_hidden = tflag("overlays", "hidden")

    caps = {}  # source key -> cv2.VideoCapture (full-res for export)
    for key, src in project["sources"].items():
        p = resolve(pdir, src.get("path"))
        if p and Path(p).is_file():
            caps[key] = open_capture(p)

    needs_bg = any(c.get("bg") for c in project["clips"])
    remover = BgRemover() if needs_bg else None
    bg_images = {}
    for c in project["clips"]:
        bg = c.get("bg") or {}
        if bg.get("image"):
            p = resolve(pdir, bg["image"])
            if p and p not in bg_images:
                im = cv2.imread(p)
                if im is not None:
                    bg_images[p] = cover_box(im, W, H)

    # Face retouch was REMOVED from the app (2026-08-04, at Renato's call after
    # four rounds that never looked good enough). The `rt` block is deliberately
    # left untouched in every project.json so nothing he set is lost — the
    # renderer simply does not read it. render/retouch.py is still in the repo;
    # bringing the feature back means wiring these two lines and the Inspector
    # card again, not rewriting it.

    ov_videos, ov_images = {}, {}
    for o in project.get("overlays", []):
        if o.get("kind") == "video":
            p = resolve(pdir, o.get("path"))
            if p and Path(p).is_file():
                ov_videos[o["id"]] = OverlayVideo(p, fps)
            elif not quiet:
                print(f"overlay {o['id']}: video missing ({p}) — skipped", flush=True)
        else:
            use = o.get("cutPath") if (o.get("cut") and o.get("cutPath")) else o.get("path")
            p = resolve(pdir, use)
            if not p or not Path(p).is_file():
                if not quiet:
                    print(f"overlay {o['id']}: image missing ({p}) — skipped", flush=True)
                continue
            img = Image.open(p).convert("RGBA")
            if o.get("cut") and not o.get("cutPath"):
                from rembg import remove as _rm
                img = _rm(img)
            ov_images[o["id"]] = img

    log_path = Path(out_path).with_suffix(".log")
    log = open(log_path, "wb")
    ff = subprocess.Popen([
        FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
        "-r", f"{fps:.6f}", "-i", "-",
        *encoder_args(enc_kind, W, H, fps, args.scale),
        "-f", "matroska", str(out_path),
    ], stdin=subprocess.PIPE, stderr=log)

    clip_starts = [clip_out_start(project, i) for i in range(len(project["clips"]))]
    ov_radius = int(round(10 / REF_H * H))
    cur_clip = -1
    # source-anchored captions are static across the render → materialize once,
    # then clip each group's tail at the next group's start (REN-116)
    mat_caps = [mc for c in project.get("captions", [])
                if (mc := materialize_cap(project, c)) is not None]
    cap_bounds = caption_bounds(mat_caps)
    ov_cache = {}  # (caption, spoken-word mask) -> (x, y, RGBA crop) | None

    # chunk starting mid-clip: reproduce the serial decode state (seek once at
    # the clip's first frame, then read forward) so we land on the same frame
    if f0 > 0 and not video_hidden:
        ci0, _ = out_to_source(project, f0 / fps)
        if ci0 is not None:
            fstart = clip_first_frame(project, ci0, fps, f0)
            if fstart < f0:
                cap0 = caps.get(project["clips"][ci0].get("src", "main"))
                if cap0 is not None:
                    st0 = out_to_source(project, fstart / fps)[1]
                    cap0.set(cv2.CAP_PROP_POS_MSEC, st0 * 1000.0)
                    for _ in range(f0 - fstart):
                        if not cap0.grab():
                            break
                cur_clip = ci0

    n_local = f1 - f0
    parent_pid = os.getppid()
    for fi in range(f0, f1):
        t = fi / fps
        ci, st = out_to_source(project, t)
        if ci is None:
            break
        clip = project["clips"][ci]
        cap = caps.get(clip.get("src", "main"))
        if ci != cur_clip:
            cur_clip = ci
            if remover:
                remover.prev = None
            if cap:
                cap.set(cv2.CAP_PROP_POS_MSEC, st * 1000.0)
        frame = None
        if cap and not video_hidden:  # hidden video track → black frame
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, st * 1000.0 - 50))
                ok, frame = cap.read()
                if not ok:
                    frame = None
        if frame is None:
            frame = np.zeros((H, W, 3), np.uint8)
        if frame.shape[1] != W or frame.shape[0] != H:
            frame = cover_box(frame, W, H)

        bg = clip.get("bg")
        if bg and remover is not None and not video_hidden:
            m = remover.mask(frame, W, H, bg.get("feather", 4) * args.scale)
            bgp = resolve(pdir, bg.get("image")) if bg.get("image") else None
            if bgp and bgp in bg_images:
                back = bg_images[bgp]
            else:
                back = np.zeros((H, W, 3), np.uint8)
                back[:] = hex_rgb(bg.get("color", "#000000"))[::-1]
            frame = (frame.astype(np.float32) * m + back.astype(np.float32) * (1 - m)).astype(np.uint8)

        z = zoom_at(clip, t - clip_starts[ci])
        if z and abs(z[0] - 1.0) > 1e-3 and not video_hidden:
            sc, cx, cy = z
            M = cv2.getRotationMatrix2D((cx * W, cy * H), 0, sc)
            frame = cv2.warpAffine(frame, M, (W, H), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_REFLECT)

        # overlays (timeline time) — composited one by one in array order so
        # video/image overlays stack exactly like the preview's DOM order;
        # overhang is cropped in place (mirrors the frame's overflow:hidden)
        def blend_rgba(dst, ov_np, x0, y0):
            bh_, bw_ = ov_np.shape[:2]
            cx0, cy0 = max(0, -x0), max(0, -y0)
            cx1, cy1 = min(bw_, W - x0), min(bh_, H - y0)
            if cx1 <= cx0 or cy1 <= cy0:
                return
            piece = ov_np[cy0:cy1, cx0:cx1]
            al_ = piece[:, :, 3:4].astype(np.float32) / 255.0
            if not al_.any():
                return
            rgb = piece[:, :, :3][:, :, ::-1].astype(np.float32)
            X0, Y0 = max(0, x0), max(0, y0)
            roi = dst[Y0:Y0 + (cy1 - cy0), X0:X0 + (cx1 - cx0)].astype(np.float32)
            dst[Y0:Y0 + (cy1 - cy0), X0:X0 + (cx1 - cx0)] = np.clip(
                roi * (1 - al_) + rgb * al_, 0, 255).astype(np.uint8)

        for o in ([] if ov_hidden else project.get("overlays", [])):
            t0, t1 = float(o.get("t0", 0)), float(o.get("t1") if o.get("t1") is not None else out_dur)
            if not (t0 <= t < t1):
                continue
            a = item_alpha(t, t0, t1, o.get("fadeIn", 0), o.get("fadeOut", 0))
            bw = max(2, int(float(o.get("w", 30)) / 100.0 * W))
            if o.get("kind") == "video":
                if o["id"] not in ov_videos:
                    continue
                fr = ov_videos[o["id"]].at(t - t0)
                if fr is None:
                    continue
                bh = max(2, int(float(o["h"]) / 100.0 * H) if o.get("h")
                         else int(bw * fr.shape[0] / fr.shape[1]))
                fr = cover_box(fr, bw, bh, o.get("crop", "center"))
                x0 = int(float(o.get("x", 50)) / 100.0 * W - bw / 2)
                y0 = int(float(o.get("y", 50)) / 100.0 * H - bh / 2)
                al = rounded_mask(bw, bh, ov_radius) * a
                ov_np = np.dstack([fr[:, :, ::-1], (al[:, :, 0] * 255).astype(np.uint8)])
                blend_rgba(frame, ov_np, x0, y0)
            else:
                if o["id"] not in ov_images:
                    continue
                img = ov_images[o["id"]]
                if o.get("h"):  # explicit box: cover-crop like the preview's object-fit
                    bh = max(2, int(float(o["h"]) / 100.0 * H))
                    sc = max(bw / img.width, bh / img.height)
                    rw, rh = max(1, round(img.width * sc)), max(1, round(img.height * sc))
                    im = img.resize((rw, rh), Image.LANCZOS)
                    ox, oy = (rw - bw) // 2, (rh - bh) // 2
                    im = im.crop((ox, oy, ox + bw, oy + bh))
                else:
                    bh = int(bw * img.height / img.width)
                    im = img.resize((bw, max(2, bh)), Image.LANCZOS)
                if not o.get("cut"):  # opaque images get the rounded-corner crop
                    mask = Image.new("L", im.size, 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        [0, 0, im.width - 1, im.height - 1], radius=ov_radius, fill=255)
                    old = im.split()[3]
                    im.putalpha(Image.composite(old, Image.new("L", im.size, 0), mask))
                if a < 0.999:
                    al = im.split()[3].point(lambda v: int(v * a))
                    im = im.copy()
                    im.putalpha(al)
                x0 = int(float(o.get("x", 50)) / 100.0 * W - bw / 2)
                y0 = int(float(o.get("y", 50)) / 100.0 * H - im.height / 2)
                blend_rgba(frame, np.array(im), x0, y0)

        # headlines first, then captions on top (mirrors the preview's DOM order)
        texts_now = [] if texts_hidden else [x for x in project.get("texts", [])
                                             if x["t0"] <= t < x["t1"]]
        cap_now = None if caps_hidden else pick_caption_at(cap_bounds, t)
        blob = None
        if texts_now or cap_now is not None:  # nothing to draw → skip the RGBA pass
            # A caption tile depends on t ONLY through which word is "spoken", so
            # consecutive frames inside one word reuse the identical RGBA crop
            # (headline fades change every frame, so those are never cached).
            key = None
            if cap_now is not None and not texts_now:
                key = (id(cap_now), tuple(w["t"] <= t < w["t"] + w["d"]
                                          for w in (cap_now.get("words") or ())))
                blob = ov_cache.get(key, _MISS)
            if blob is _MISS or key is None:
                overlay_pil = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                for x in texts_now:
                    a = item_alpha(t, x["t0"], x["t1"], x.get("fadeIn", 0), x.get("fadeOut", 0))
                    draw_text_block(overlay_pil, x, W, H, alpha=a)
                # captions: at most one at a time (tail clipped at the next group's
                # start + most-recent-start safety net) so groups never overlap
                if cap_now is not None:
                    draw_caption(overlay_pil, cap_now, t, W, H)
                # composite only the drawn region: everywhere else alpha is 0, so
                # frame*(1-0)+rgb*0 == frame exactly. Same pixels, ~7x less traffic.
                bb = overlay_pil.getbbox()
                blob = None
                if bb is not None:
                    ov = np.array(overlay_pil.crop(bb))
                    if ov[:, :, 3].any():
                        blob = (bb[0], bb[1], ov)
                if key is not None:
                    if len(ov_cache) > 4:  # kept small: at 4K a tile is several MB
                        ov_cache.clear()
                    ov_cache[key] = blob
        if blob is not None:
            ox0, oy0, ov = blob
            oh, ow = ov.shape[:2]
            al = ov[:, :, 3:4].astype(np.float32) / 255.0
            rgb = ov[:, :, :3][:, :, ::-1].astype(np.float32)
            roi = frame[oy0:oy0 + oh, ox0:ox0 + ow].astype(np.float32)
            frame[oy0:oy0 + oh, ox0:ox0 + ow] = np.clip(
                roi * (1 - al) + rgb * al, 0, 255).astype(np.uint8)

        # clip fade to black darkens the WHOLE frame (captions included) — the
        # preview mirrors this with a black veil over the full stage
        rel = t - clip_starts[ci]
        d = clip["out"] - clip["in"]
        f = 1.0
        if clip.get("fadeIn") and rel < clip["fadeIn"]:
            f = min(f, rel / clip["fadeIn"])
        if clip.get("fadeOut") and rel > d - clip["fadeOut"]:
            f = min(f, max(0.0, (d - rel) / clip["fadeOut"]))
        if f < 0.999:
            frame = (frame.astype(np.float32) * f).astype(np.uint8)

        ff.stdin.write(frame.tobytes())
        if (fi - f0) % 30 == 0:
            print(f"PROGRESS {fi - f0 if quiet else fi}/{n_local if quiet else n_frames}", flush=True)
            if quiet and os.getppid() != parent_pid:  # parent gone: don't run on alone
                ff.stdin.close()
                ff.wait()
                sys.exit("render parent exited — worker aborting")

    for cap in caps.values():
        cap.release()
    ff.stdin.close()
    rc = ff.wait()
    log.close()
    if rc != 0:
        print(log_path.read_text(errors="ignore")[-1200:], flush=True)
        sys.exit(1)
    print(f"PROGRESS {n_local if quiet else f1}/{n_local if quiet else n_frames}", flush=True)


def run_workers(chunks, args, tmpdir, enc_kind, n_frames):
    """One subprocess per chunk (cv2 capture handles are not fork-safe, and a
    separate interpreter also dodges the GIL). Returns the segment paths in
    timeline order. Worker progress is summed into the same "PROGRESS a/b"
    line the job runner parses."""
    segs = [str(Path(tmpdir) / f"seg{k:03d}.mkv") for k in range(len(chunks))]
    env = dict(os.environ)
    env.setdefault("OMP_NUM_THREADS", "1")           # don't oversubscribe: N
    env.setdefault("OPENBLAS_NUM_THREADS", "1")      # processes already fill
    env.setdefault("VECLIB_MAXIMUM_THREADS", "1")    # every core
    env["VSTUDIO_ENCODER"] = enc_kind
    cpu = os.cpu_count() or 4
    if enc_kind == "x264":  # hardware encoding needs no threads; libx264 does
        env["VSTUDIO_ENC_THREADS"] = str(max(1, cpu // len(chunks)))
    procs, errs = [], []
    for k, (a, b) in enumerate(chunks):
        # OpenCV threads in proportion to the chunk's share of the timeline: with
        # even chunks that is 1 each (N processes already fill the machine), but a
        # clip that cannot be split (bg removal) gets a fat chunk AND
        # the threads to chew through it. Capped at the performance-core count —
        # spreading one parallel_for over the E cores makes these filters slower,
        # because every barrier waits on the slowest core.
        env["VSTUDIO_CV_THREADS"] = str(max(1, min(perf_cores(), round(cpu * (b - a) / n_frames))))
        # stderr -> file (a pipe nobody drains until wait() can deadlock)
        errs.append(open(Path(tmpdir) / f"seg{k:03d}.err", "w+"))
        procs.append(subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(args.project_dir), segs[k],
             "--scale", str(args.scale), "--worker", "--frames", f"{a}:{b}"],
            stdout=subprocess.PIPE, stderr=errs[k], text=True, env=env))

    counts = [0] * len(chunks)
    lock = threading.Lock()
    last = [0.0]

    def pump(k, proc):
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("PROGRESS "):
                try:
                    counts[k] = int(line.split()[1].split("/")[0])
                except Exception:
                    continue
                with lock:
                    now = time.time()
                    if now - last[0] > 0.2:
                        last[0] = now
                        print(f"PROGRESS {min(sum(counts), n_frames)}/{n_frames}", flush=True)
            elif line and k == 0:  # notices are identical in every worker
                print(line, flush=True)

    threads = [threading.Thread(target=pump, args=(k, p), daemon=True)
               for k, p in enumerate(procs)]
    for th in threads:
        th.start()
    fail = None
    for k, p in enumerate(procs):
        rc = p.wait()
        threads[k].join(timeout=5)
        errs[k].flush()
        errs[k].seek(0)
        err = (errs[k].read() or "")[-1200:]
        errs[k].close()
        if rc != 0 and fail is None:
            fail = f"render worker {k} (frames {chunks[k][0]}-{chunks[k][1]}) failed: {err}"
    if fail:
        for p in procs:
            if p.poll() is None:
                p.kill()
        sys.exit(fail)
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("out")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=0,
                    help="0 = auto (cores - 2). 1 disables parallel rendering.")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--frames", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    pdir = Path(args.project_dir)
    project = load_project(pdir / "project.json")

    W = int(project["w"] * args.scale) // 2 * 2
    H = int(project["h"] * args.scale) // 2 * 2
    fps = float(project.get("fps") or 30)
    out_dur = out_duration(project)
    n_frames = int(out_dur * fps)

    # ---- worker mode: render one chunk to a video-only segment and stop ----
    if args.worker:
        # split OpenCV's thread pool across the sibling workers. With many
        # chunks that is 1 thread each (they already fill the machine); with a
        # few fat chunks (a long bg-removal clip cannot be split) each worker
        # keeps enough threads to run the filters as fast as the old renderer.
        cv2.setNumThreads(int(os.environ.get("VSTUDIO_CV_THREADS") or 1))
        f0, f1 = (int(v) for v in args.frames.split(":"))
        render_range(project, pdir, args, W, H, fps, out_dur, n_frames, f0, f1,
                     args.out, os.environ.get("VSTUDIO_ENCODER", "x264"), quiet=True)
        return

    print(f"render {W}x{H} @{fps:.2f} dur={out_dur:.2f}s frames={n_frames}", flush=True)
    if not n_frames:
        sys.exit("empty project — nothing to render")

    # fail fast on missing clip sources (better than a broken mux after encode)
    for c in project["clips"]:
        src = project["sources"].get(c.get("src", "main")) or {}
        p = resolve(pdir, src.get("path"))
        if not p or not Path(p).is_file():
            sys.exit(f"clip {c['id']}: source '{c.get('src', 'main')}' not found ({p})")

    enc_kind = pick_encoder(W, H, fps, args.scale)
    n_workers = args.workers or int(os.environ.get("VSTUDIO_WORKERS") or 0) \
        or max(1, (os.cpu_count() or 2) - 2)
    if any(c.get("bg") for c in project["clips"]):
        n_workers = min(n_workers, 4)  # one rembg/onnx session per worker is heavy
    chunks = plan_chunks(project, n_frames, fps, n_workers)
    print(f"encoder={'h264_videotoolbox' if enc_kind == 'vt' else 'libx264'} "
          f"chunks={len(chunks)} {[b - a for a, b in chunks]}", flush=True)

    tmpdir = tempfile.mkdtemp(prefix="vstudio_")
    if len(chunks) == 1:
        segs = [str(Path(tmpdir) / "video.mkv")]
        render_range(project, pdir, args, W, H, fps, out_dur, n_frames,
                     0, n_frames, segs[0], enc_kind)
    else:
        segs = run_workers(chunks, args, tmpdir, enc_kind, n_frames)

    inputs, afilter = build_audio_filter(project, pdir, out_dur, tmpdir)
    cmd = [FFMPEG, "-y"]
    if len(segs) == 1:
        cmd += ["-i", segs[0]]
    else:  # concat demuxer: identical codec params in every segment -> -c copy
        lst = Path(tmpdir) / "segments.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in segs))
        cmd += ["-f", "concat", "-safe", "0", "-i", str(lst)]
    for i in inputs:
        cmd += ["-i", i]
    # audio filter refers to inputs shifted by 1 (video first) -> remap
    script = Path(afilter).read_text()
    for k in range(len(inputs), 0, -1):
        script = script.replace(f"[{k - 1}:a]", f"[__{k}:a]")
    script = script.replace("[__", "[")
    Path(afilter).write_text(script)
    # Mux to a .part file and rename on success. Written straight to the final
    # name, an export killed mid-mux (the app restarting, the machine sleeping,
    # a crash) left a truncated mp4 sitting at exactly the filename a finished
    # render would have — it looks done, it plays for four seconds, and the app
    # gets the blame. os.replace is atomic within a filesystem.
    part = str(Path(args.out).with_suffix(".part.mp4"))
    cmd += ["-filter_complex_script", afilter, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", part]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        Path(part).unlink(missing_ok=True)
        sys.exit(1)
    os.replace(part, args.out)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"PROGRESS {n_frames}/{n_frames}", flush=True)
    print(f"done: {args.out}", flush=True)


if __name__ == "__main__":
    main()
