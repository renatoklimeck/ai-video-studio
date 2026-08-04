// Schema-v2 helpers — time mapping must mirror render/common.py exactly.

export function outDuration(project) {
  return (project.clips || []).reduce((a, c) => a + (c.out - c.in), 0)
}

export function outToSource(project, t) {
  let acc = 0
  const clips = project.clips || []
  for (let i = 0; i < clips.length; i++) {
    const d = clips[i].out - clips[i].in
    if (t < acc + d || i === clips.length - 1) {
      return [i, Math.min(clips[i].out, clips[i].in + Math.max(0, t - acc))]
    }
    acc += d
  }
  return [null, null]
}

export function clipOutStart(project, idx) {
  return (project.clips || []).slice(0, idx).reduce((a, c) => a + (c.out - c.in), 0)
}

// timeline seconds -> [source key, source seconds] (or [null, null])
export function outToSourceKey(project, t) {
  const [i, srcT] = outToSource(project, t)
  if (i == null) return [null, null]
  return [project.clips[i].src || 'main', srcT]
}

// source seconds of a given source -> timeline seconds (first clip covering it),
// or null if that source-time is not on the timeline anymore
export function sourceToTimeline(project, srcKey, srcT) {
  let acc = 0
  for (const c of project.clips || []) {
    const d = c.out - c.in
    if ((c.src || 'main') === srcKey && srcT >= c.in - 1e-4 && srcT <= c.out + 1e-4) {
      return acc + Math.min(d, Math.max(0, srcT - c.in))
    }
    acc += d
  }
  return null
}

// Source-anchored captions (REN-115): a group carries capAnchor:"source",
// src, and words {w, t0, t1} in SOURCE seconds. The timeline position is
// DERIVED at draw time — a word renders iff its source moment is covered by a
// clip of that source; cut it and the word vanishes, the rest reflow. This
// materializes a source-anchored group into the legacy timeline shape
// (words {w,t,d}) so the SAME draw code (preview + renderer) handles both.
// Returns null when no word survives (all cut) — caller drops the group.
export function materializeCap(project, cap) {
  if (cap.capAnchor !== 'source') return cap // legacy timeline-anchored
  const src = cap.src || 'main'
  const words = []
  for (const w of cap.words || []) {
    const tl0 = sourceToTimeline(project, src, w.t0)
    if (tl0 == null) continue // this source moment was cut → not shown
    let tl1 = sourceToTimeline(project, src, w.t1)
    if (tl1 == null || tl1 <= tl0) tl1 = tl0 + Math.max(0.05, w.t1 - w.t0)
    words.push({ w: w.w, t: +tl0.toFixed(2), d: +(tl1 - tl0).toFixed(2) })
  }
  if (!words.length) return null
  return { ...cap, words }
}

// distribute text evenly over a SOURCE span [t0,t1] → words {w,t0,t1} (source)
export function mkWordsSrc(text, t0, t1) {
  const parts = text.split(/\s+/).filter(Boolean)
  if (!parts.length) return []
  const d = (t1 - t0) / parts.length
  return parts.map((w, i) => ({ w, t0: +(t0 + i * d).toFixed(2), t1: +(t0 + (i + 1) * d).toFixed(2) }))
}

// Distribute text over ONLY the source sub-ranges of `src` actually covered by
// clips within [t0,t1] (REN-115 fix). A raw [t0,t1] span can straddle a cut
// (editing a group that spans a clip boundary) or overrun a short clip (adding),
// which would drop the words that land in the gap; laying them across the
// concatenated COVERED length guarantees every word stays on the timeline.
export function mkWordsSrcCovered(project, src, text, t0, t1) {
  const parts = text.split(/\s+/).filter(Boolean)
  if (!parts.length) return []
  const ranges = []
  for (const c of project?.clips || []) {
    if ((c.src || 'main') !== src) continue
    const a = Math.max(c.in, t0), b = Math.min(c.out, t1)
    if (b > a) ranges.push([a, b])
  }
  ranges.sort((x, y) => x[0] - y[0])
  const L = ranges.reduce((sum, [a, b]) => sum + (b - a), 0)
  if (!ranges.length || L <= 0) return mkWordsSrc(text, t0, t1) // nothing covered: raw fallback
  const at = (f) => {
    let rem = f * L
    for (const [a, b] of ranges) { const len = b - a; if (rem <= len) return a + rem; rem -= len }
    return ranges[ranges.length - 1][1]
  }
  const n = parts.length
  return parts.map((w, i) => ({ w, t0: +at(i / n).toFixed(2), t1: +at((i + 1) / n).toFixed(2) }))
}

// Piecewise smoothstep over kfs (cx/cy in % 0-100) — same as render/common.py.
export function zoomAt(clip, relT) {
  const kfs = (clip.kfs || []).slice().sort((a, b) => a.t - b.t)
  if (!kfs.length) return null
  const un = (k) => ({ scale: k.scale, cx: (k.cx ?? 50) / 100, cy: (k.cy ?? 50) / 100 })
  if (relT <= kfs[0].t) return un(kfs[0])
  for (let i = 0; i < kfs.length - 1; i++) {
    const a = kfs[i], b = kfs[i + 1]
    if (relT <= b.t) {
      let p = (relT - a.t) / Math.max(1e-6, b.t - a.t)
      p = p * p * (3 - 2 * p)
      const A = un(a), B = un(b)
      return {
        scale: A.scale + (B.scale - A.scale) * p,
        cx: A.cx + (B.cx - A.cx) * p,
        cy: A.cy + (B.cy - A.cy) * p,
      }
    }
  }
  return un(kfs[kfs.length - 1])
}

export function itemAlpha(t, t0, t1, fadeIn = 0, fadeOut = 0) {
  let a = 1
  if (fadeIn && t < t0 + fadeIn) a = Math.min(a, Math.max(0, (t - t0) / fadeIn))
  if (fadeOut && t > t1 - fadeOut) a = Math.min(a, Math.max(0, (t1 - t) / fadeOut))
  return a
}

// Visible window of a caption group: [first.t, last.t + last.d + 0.15]
export function capSpan(cap) {
  const ws = cap.words || []
  if (!ws.length) return null
  return [ws[0].t, ws[ws.length - 1].t + ws[ws.length - 1].d + 0.15]
}

// ── captions never overlap (REN-156) ────────────────────────────────────────
//
// Two captions on screen at once is never something he asked for — it is always
// the result of a mutation that did not look at its neighbours (a manual add on
// top of an existing one, a drag, a stretch, a split). The rule lives here, in
// TIMELINE seconds, because that is where "at the same time" means anything:
// the words are stored in SOURCE seconds and two groups of different sources
// can hold the same source instant without ever being visible together.

/** [start, end] of a caption on the timeline, or null when it is fully cut. */
export function capTimelineSpan(project, cap) {
  const m = materializeCap(project, cap)
  return m ? capSpan(m) : null
}

/** Every caption's VISIBLE timeline span, sorted, optionally excluding one id.
 *
 * The tail is clipped at the next group's start, exactly as the preview and
 * render/common.py:caption_bounds do. Without that clip the raw spans of
 * ordinary back-to-back karaoke groups all "overlap" by the 0.15s tail — 38 of
 * 52 groups on his own project — and an overlap rule built on them would fire
 * on data that draws perfectly. */
export function capSpans(project, exceptId = null) {
  const raw = (project.captions || [])
    .filter((c) => c.id !== exceptId)
    .map((c) => { const sp = capTimelineSpan(project, c); return sp && { id: c.id, a: sp[0], b: sp[1] } })
    .filter(Boolean)
    .sort((x, y) => x.a - y.a)
  for (let i = 0; i < raw.length - 1; i++) raw[i].b = Math.min(raw[i].b, raw[i + 1].a)
  return raw
}

/** The room around `t` that no other caption is using: [from, to], or null. */
export function freeSlotAt(project, t, exceptId = null, want = 0.8) {
  const spans = capSpans(project, exceptId)
  const end = outDuration(project)
  let from = Math.max(0, Math.min(t, end))
  for (const s of spans) {
    if (s.b <= from + 1e-3) continue          // entirely before us
    if (s.a > from + 1e-3) {                  // a gap starts here
      const to = Math.min(s.a, end)
      if (to - from >= want) return [from, to]
    }
    from = Math.max(from, s.b)                // inside/before it — move past
  }
  return end - from >= want ? [from, end] : null
}

/** The room a caption may move or stretch into.
 *
 * Bounded by where the NEIGHBOURS BEGIN AND END, not by where they currently
 * stop drawing. Because every tail is clipped at the next start, a caption
 * pushed into its neighbour's time does not overlap it — the neighbour simply
 * ends earlier, which is what "the new one takes that space" looks like. Using
 * the drawn edges as the limit instead left zero room on a fully captioned
 * video (karaoke groups are back to back), so dragging did nothing at all.
 *
 * The margin is what stops him squeezing a neighbour off the screen entirely. */
export const CAP_MIN = 0.25

export function capLimits(project, capId, at) {
  const spans = capSpans(project, capId)
  let lo = 0, hi = outDuration(project)
  for (const s of spans) {
    if (s.a <= at) lo = Math.max(lo, s.a + CAP_MIN)     // keep the one before visible
    else { hi = Math.min(hi, s.b - CAP_MIN); break }    // and the one after
  }
  return [Math.max(0, lo), Math.max(lo + CAP_MIN, hi)]
}

// Caption re-sync: split text on whitespace, distribute evenly over [t0, t1]
export function mkWords(text, t0, t1) {
  const parts = text.split(/\s+/).filter(Boolean)
  if (!parts.length) return []
  const d = (t1 - t0) / parts.length
  return parts.map((w, i) => ({ w, t: +(t0 + i * d).toFixed(2), d: +d.toFixed(2) }))
}

export const FONTS_CSS = {
  'Helvetica Bold': { family: 'Helvetica, Arial, sans-serif', weight: 700 },
  'Helvetica': { family: 'Helvetica, Arial, sans-serif', weight: 400 },
  'Arial Rounded': { family: "'Arial Rounded MT Bold', Arial, sans-serif", weight: 700 },
  'Arial Black': { family: "'Arial Black', Arial, sans-serif", weight: 900 },
  'Arial Bold': { family: 'Arial, sans-serif', weight: 700 },
}
export const FONT_NAMES = Object.keys(FONTS_CSS)

export function fmtTime(t) {
  if (t == null || isNaN(t)) return '0:00.0'
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${m}:${s < 10 ? '0' : ''}${s.toFixed(1)}`
}

export function ago(ts) {
  const d = Date.now() - ts
  if (d < 90000) return 'just now'
  if (d < 3600000) return `${Math.round(d / 60000)} min ago`
  if (d < 86400000) return `${Math.round(d / 3600000)} h ago`
  return `${Math.round(d / 86400000)} d ago`
}

export function fmtWhen(ts) {
  const d = new Date(ts)
  const now = new Date()
  const hm = `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`
  if (d.toDateString() === now.toDateString()) return `today · ${hm}`
  return `${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} · ${hm}`
}

export const uid = (p = '') => p + Math.random().toString(36).slice(2, 9)

// ---- track controls (REN-112) ----
// sel.type / timeline lane key -> tracks{} key
export const TRACK_KEY = { clip: 'video', cap: 'captions', text: 'texts', ov: 'overlays', aud: 'audio' }
export function trackFlag(project, trackKey, flag) {
  return !!(project?.tracks?.[trackKey]?.[flag])
}
// does the timeline lane / selection type belong to a locked track?
export function typeLocked(project, type) {
  const k = TRACK_KEY[type]
  return !!(k && trackFlag(project, k, 'locked'))
}
