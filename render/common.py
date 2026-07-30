"""Shared schema-v2 helpers: time mapping, fonts, caption/headline drawing.
The project JSON is the single source of truth for the app preview AND this
renderer, so anything drawn here must match the web preview's layout math:
  - x, y are CENTER-anchored percentages (0-100) of project width/height
  - size is a percentage of project HEIGHT
  - caption words: {w, t, d} in TIMELINE seconds; group visible from
    words[0].t to last.t + last.d + 0.15 (same tail as the preview)
  - captions wrap at 86% of frame width, word gap 0.32em, line-height 1.25,
    spoken word in caption color, others rgba(255,255,255,dim)
  - CSS px effects (shadows, radii) scale with height, reference 640px
"""
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFilter import GaussianBlur

REF_H = 640.0  # CSS reference preview height for px-based effects

# Font resolution: first existing candidate wins. macOS paths first, then
# metric-compatible Linux fallbacks (Liberation Sans ~ Helvetica/Arial) so the
# renderer also runs on the VPS (apt: fonts-liberation fonts-noto-color-emoji).
_FONT_CANDIDATES = {
    "Helvetica Bold": [
        ("/System/Library/Fonts/Helvetica.ttc", 1),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 0),
    ],
    "Helvetica": [
        ("/System/Library/Fonts/Helvetica.ttc", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 0),
    ],
    "Arial Rounded": [
        ("/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 0),
    ],
    "Arial Black": [
        ("/System/Library/Fonts/Supplemental/Arial Black.ttf", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 0),
    ],
    "Arial Bold": [
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 0),
    ],
}
FONTS = {name: next(((p, i) for p, i in cands if Path(p).exists()), cands[-1])
         for name, cands in _FONT_CANDIDATES.items()}
DEFAULT_FONT = "Helvetica Bold"

EMOJI_FONT = next((p for p in (
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
) if Path(p).exists()), "/System/Library/Fonts/Apple Color Emoji.ttc")

_font_cache = {}
_emoji_cache = {}

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF️]+"
)


def load_font(name, px):
    key = (name, px)
    if key not in _font_cache:
        path, idx = FONTS.get(name, FONTS[DEFAULT_FONT])
        _font_cache[key] = ImageFont.truetype(path, px, index=idx)
    return _font_cache[key]


def emoji_img(chars, px):
    key = (chars, px)
    if key not in _emoji_cache:
        f = None
        # 109 is Noto Color Emoji's only bitmap strike on Linux
        for strike in (160, 137, 109, 96, 64, 48, 40, 32, 26, 20):
            try:
                f = ImageFont.truetype(EMOJI_FONT, strike)
                break
            except OSError:
                continue
        if f is None:
            _emoji_cache[key] = None
        else:
            im = Image.new("RGBA", (int(strike * (len(chars) + 1)), int(strike * 1.4)), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((0, 0), chars, font=f, embedded_color=True)
            b = im.getbbox()
            if b:
                im = im.crop(b)
                h = px
                w = max(1, int(im.width * h / im.height))
                _emoji_cache[key] = im.resize((w, h), Image.LANCZOS)
            else:
                _emoji_cache[key] = None
    return _emoji_cache[key]


def split_runs(text):
    """Split a line into [(kind, chunk)] runs where kind is 'emoji'|'text'."""
    runs, pos = [], 0
    for m in EMOJI_RE.finditer(text):
        if m.start() > pos:
            runs.append(("text", text[pos:m.start()]))
        runs.append(("emoji", m.group()))
        pos = m.end()
    if pos < len(text):
        runs.append(("text", text[pos:]))
    return runs or [("text", "")]


def hex_rgb(h, default=(255, 255, 255)):
    try:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


# ---------- time mapping (mirrors web/src/time.js exactly) ----------

def out_duration(project):
    return sum(c["out"] - c["in"] for c in project["clips"])


def out_to_source(project, t):
    """timeline seconds -> (clip index, source seconds) or (None, None)."""
    acc = 0.0
    clips = project["clips"]
    for i, c in enumerate(clips):
        d = c["out"] - c["in"]
        if t < acc + d or i == len(clips) - 1:
            return i, min(c["out"], c["in"] + max(0.0, t - acc))
        acc += d
    return None, None


def clip_out_start(project, idx):
    return sum(c["out"] - c["in"] for c in project["clips"][:idx])


def zoom_at(clip, rel_t):
    """Piecewise smoothstep interpolation over kfs (cx/cy in % 0-100).
    With the common 2-keyframe case this equals the handoff's first->last lerp."""
    kfs = sorted(clip.get("kfs") or [], key=lambda k: k["t"])
    if not kfs:
        return None
    def unpack(k):
        return k["scale"], k.get("cx", 50) / 100.0, k.get("cy", 50) / 100.0
    if rel_t <= kfs[0]["t"]:
        return unpack(kfs[0])
    for a, b in zip(kfs, kfs[1:]):
        if rel_t <= b["t"]:
            p = (rel_t - a["t"]) / max(1e-6, b["t"] - a["t"])
            p = p * p * (3 - 2 * p)  # smoothstep
            sa, xa, ya = unpack(a)
            sb, xb, yb = unpack(b)
            return sa + (sb - sa) * p, xa + (xb - xa) * p, ya + (yb - ya) * p
    return unpack(kfs[-1])


def item_alpha(t, t0, t1, fade_in=0.0, fade_out=0.0):
    a = 1.0
    if fade_in and t < t0 + fade_in:
        a = min(a, max(0.0, (t - t0) / fade_in))
    if fade_out and t > t1 - fade_out:
        a = min(a, max(0.0, (t1 - t) / fade_out))
    return a


def cap_span(cap):
    """Visible window of a caption group: [first.t, last.t + last.d + 0.15]."""
    ws = cap.get("words") or []
    if not ws:
        return None
    return ws[0]["t"], ws[-1]["t"] + ws[-1]["d"] + 0.15


# ---------- source-anchored captions (REN-115; mirrors web/src/time.js) ----------

def source_to_timeline(project, src_key, src_t):
    """SOURCE seconds of a given source -> timeline seconds (first clip covering
    it), or None if that source-time isn't on the timeline. Verbatim port of the
    JS sourceToTimeline so preview == export."""
    acc = 0.0
    for c in project["clips"]:
        d = c["out"] - c["in"]
        if (c.get("src", "main")) == src_key and c["in"] - 1e-4 <= src_t <= c["out"] + 1e-4:
            return acc + min(d, max(0.0, src_t - c["in"]))
        acc += d
    return None


def caption_bounds(mat_caps):
    """Visible [start, end] per materialized caption group, with the +0.15s tail
    CLIPPED at the next group's start (REN-116): karaoke groups are back-to-back,
    so an unclipped tail overlaps the next group. Returns [(start, end, cap)]."""
    raw = [[sp[0], sp[1], c] for c in mat_caps if (sp := cap_span(c))]
    starts = sorted(r[0] for r in raw)
    out = []
    for s0, s1, c in raw:
        nxt = [st for st in starts if st > s0 + 1e-4]
        out.append((s0, min(s1, nxt[0]) if nxt else s1, c))
    return out


def pick_caption_at(bounds, t):
    """At most ONE caption at time t — the one whose start is most recent
    (argmax start <= t among the visible), a safety net for any residual overlap."""
    vis = [(s0, c) for (s0, e, c) in bounds if s0 <= t <= e]
    if not vis:
        return None
    vis.sort(key=lambda x: x[0])
    return vis[-1][1]


def materialize_cap(project, cap):
    """Fold a source-anchored group (words {w,t0,t1} in SOURCE seconds + src)
    into the legacy timeline shape (words {w,t,d}) so the SAME draw code runs.
    A word survives iff its source moment is covered by a clip. Returns None when
    all words are cut. Legacy timeline-anchored groups pass through unchanged."""
    if cap.get("capAnchor") != "source":
        return cap
    src = cap.get("src", "main")
    words = []
    for w in cap.get("words", []):
        tl0 = source_to_timeline(project, src, w["t0"])
        if tl0 is None:
            continue
        tl1 = source_to_timeline(project, src, w["t1"])
        if tl1 is None or tl1 <= tl0:
            tl1 = tl0 + max(0.05, w["t1"] - w["t0"])
        words.append({"w": w["w"], "t": round(tl0, 2), "d": round(tl1 - tl0, 2)})
    if not words:
        return None
    out = dict(cap)
    out["words"] = words
    return out


# ---------- drawing (must mirror the web preview) ----------

def draw_caption(overlay, cap, t, W, H):
    """Karaoke/static caption group at timeline time t.
    CSS mirror: block centered at (x%,y%), width maxW% of W (default 86),
    flex-wrap centered, word gap 0.32em, line-height 1.25, weight 700,
    shadow 0 2px 10px rgba(0,0,0,.7)."""
    px = max(8, int(round(cap.get("size", 3.4) / 100.0 * H)))
    f = load_font(cap.get("font", DEFAULT_FONT), px)
    color = hex_rgb(cap.get("color", "#FFFFFF"))
    dim = float(cap.get("dim", 0.45))
    karaoke = cap.get("mode", "karaoke") == "karaoke"
    words = cap.get("words") or []
    if not words:
        return

    meas = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    gap = 0.32 * px
    max_w = (cap.get("maxW") or 86) / 100.0 * W
    widths = [meas.textlength(w["w"], font=f) for w in words]

    # greedy wrap (flex-wrap)
    lines, cur, cur_w = [], [], 0.0
    for w, wd in zip(words, widths):
        add = wd if not cur else wd + gap
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w = [(w, wd)], wd
        else:
            cur.append((w, wd))
            cur_w += add
    if cur:
        lines.append(cur)

    lh = 1.25 * px
    asc, desc = f.getmetrics()
    total_h = lh * len(lines)
    top = cap.get("y", 76) / 100.0 * H - total_h / 2
    scale = H / REF_H
    sh_off, sh_blur = 2 * scale, 10 * scale

    tile = Image.new("RGBA", (W, int(total_h + sh_blur * 4 + px)), (0, 0, 0, 0))
    y_pad = sh_blur * 2
    d = ImageDraw.Draw(tile)
    sh = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    ds = ImageDraw.Draw(sh)
    for li, line in enumerate(lines):
        line_w = sum(wd for _, wd in line) + gap * (len(line) - 1)
        x = cap.get("x", 50) / 100.0 * W - line_w / 2
        y = y_pad + li * lh + (lh - (asc + desc)) / 2
        for w, wd in line:
            spoken = (not karaoke) or (w["t"] <= t < w["t"] + w["d"])
            fill = color + (255,) if spoken else (255, 255, 255, int(255 * dim))
            ds.text((x, y), w["w"], font=f, fill=(0, 0, 0, int(255 * 0.7)))
            d.text((x, y), w["w"], font=f, fill=fill)
            x += wd + gap
    sh = sh.filter(GaussianBlur(max(1, sh_blur / 2)))
    base = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    base.alpha_composite(sh, (0, int(sh_off)))
    base.alpha_composite(tile)
    overlay.alpha_composite(base, (0, int(top - y_pad)))


def _run_width(runs, f, px, d0):
    wsum = 0
    for kind, chunk in runs:
        if kind == "text":
            wsum += d0.textlength(chunk, font=f)
        else:
            em = emoji_img(chunk, px)
            wsum += (em.width if em else 0) + int(px * 0.15)
    return wsum


def draw_text_block(overlay, item, W, H, alpha=1.0):
    """Headline: multi-line centered text at (x%,y%), weight 700, line-height 1.2,
    optional soft shadow 0 3px 14px rgba(0,0,0,0.65), inline emoji support.
    Manual \\n breaks are kept; long lines soft-wrap at maxW% of the frame
    width (default 90 — mirrors the preview's .pv-text max-width)."""
    px = max(8, int(round(item.get("size", 4.2) / 100.0 * H)))
    f = load_font(item.get("font", "Arial Rounded"), px)
    color = hex_rgb(item.get("color", "#FFFFFF"))
    d0 = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    asc, desc = f.getmetrics()
    lh = int(px * 1.2)
    max_w = (item.get("maxW") or 90) / 100.0 * W
    space_w = d0.textlength(" ", font=f)

    lines = []
    for manual in item.get("text", "").split("\n"):
        words = manual.split(" ")
        cur, cur_w = [], 0.0
        for word in words:
            w_w = _run_width(split_runs(word), f, px, d0)
            add = w_w if not cur else w_w + space_w
            if cur and cur_w + add > max_w:
                lines.append(" ".join(cur))
                cur, cur_w = [word], w_w
            else:
                cur.append(word)
                cur_w += add
        lines.append(" ".join(cur))

    measured = []
    for ln in lines:
        runs = split_runs(ln)
        measured.append((runs, _run_width(runs, f, px, d0)))
    total_h = lh * len(lines)
    scale = H / REF_H
    sh_off, sh_blur = 3 * scale, 14 * scale
    pad = int(sh_blur * 2 + px * 0.2)
    tw = int(max(w for _, w in measured)) if measured else 4
    tile = Image.new("RGBA", (tw + 2 * pad, total_h + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    d, ds = ImageDraw.Draw(tile), ImageDraw.Draw(sh)
    for i, (runs, wsum) in enumerate(measured):
        x = (tile.width - wsum) / 2
        y = pad + i * lh + (lh - (asc + desc)) / 2
        for kind, chunk in runs:
            if kind == "text":
                if item.get("shadow", True):
                    ds.text((x, y), chunk, font=f, fill=(0, 0, 0, int(255 * 0.65)))
                d.text((x, y), chunk, font=f, fill=color + (255,))
                x += d0.textlength(chunk, font=f)
            else:
                em = emoji_img(chunk, px)
                if em is not None:
                    tile.alpha_composite(em, (int(x + px * 0.1), int(y + (asc - px) * 0.5 + px * 0.05)))
                    x += em.width + int(px * 0.15)
    if item.get("shadow", True):
        sh = sh.filter(GaussianBlur(max(1, sh_blur / 2)))
        base = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        base.alpha_composite(sh, (0, int(sh_off)))
        base.alpha_composite(tile)
        tile = base
    if alpha < 1.0:
        a = tile.split()[3].point(lambda v: int(v * alpha))
        tile.putalpha(a)
    x0 = int(item.get("x", 50) / 100.0 * W - tile.width / 2)
    y0 = int(item.get("y", 50) / 100.0 * H - tile.height / 2)
    overlay.alpha_composite(tile, (x0, y0))


def load_project(path):
    p = json.loads(Path(path).read_text())
    if p.get("version") != 2:
        raise SystemExit(f"{path}: not a v2 project — run scripts/migrate_v2.py first")
    p.setdefault("clips", [])
    p.setdefault("captions", [])
    p.setdefault("texts", [])
    p.setdefault("overlays", [])
    p.setdefault("audios", [])
    p.setdefault("sources", {})
    p.setdefault("tracks", {})
    return p
