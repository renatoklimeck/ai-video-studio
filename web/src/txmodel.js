// Transcript view model (REN-83): fold the project's clips + per-source
// transcripts into a Descript-style reading order. A word is KEPT when a clip
// owns it, DELETED (struck) otherwise. Take headers mark where each clip
// begins. Nothing here is stored — it's derived from clips + transcripts so it
// can never desync.
//
// Reading order is TIMELINE order (REN-165). It used to be SOURCE order: the
// words of a source were walked once, start to finish, and a take header was
// emitted whenever the covering clip changed. That reads the video correctly
// only while the clips happen to sit in the same order as the footage. After an
// AI edit they do not — the best take is moved to the front — so the panel read
// the video out of sequence, which is what "partes batem e outras nem um pouco"
// was. It also broke the caller: Transcript.jsx binary-searches the kept words
// by timelineT, and that array was not sorted by timelineT, so the highlight
// jumped to arbitrary words.
import { clipOutStart } from './time'

const srcOf = (c) => c.src || 'main'

// Which clip owns a word. Verbatim port of the rule in
// render/captions_from_transcript.py so the panel, the caption generator and
// the export agree on one answer. It is overlap-based, NOT midpoint-based:
// whisper stamps a word's t0 at the END of the previous word, i.e. back in the
// silence before the take, so a midpoint rule keeps dropping the OPENING word
// of a line. The panel never got that fix and was still using the midpoint.
export function ownerMap(project, transcripts) {
  const clips = project?.clips || []
  const bySrc = new Map()
  clips.forEach((c, index) => {
    const src = srcOf(c)
    if (!bySrc.has(src)) bySrc.set(src, [])
    bySrc.get(src).push({ clip: c, index })
  })

  const owner = new Map()   // `${src}|${wordIndex}` -> clip index
  for (const [src, list] of bySrc) {
    const words = transcripts?.[src]?.words || []
    words.forEach((w, i) => {
      let best = null, bestOv = 0
      // enough of the word must be inside the clip to be HEARD there: half of
      // it, or 120 ms, whichever is less. A bare sliver would let the tail of
      // an abandoned attempt print on a take that never says it; demanding a
      // full half alone throws away legitimate opening words.
      const need = Math.min((w.t1 - w.t0) * 0.5, 0.12)
      for (const { clip, index } of list) {
        const ov = Math.min(w.t1, clip.out) - Math.max(w.t0, clip.in)
        if (ov > bestOv && ov >= need) { best = index; bestOv = ov }
      }
      if (best == null) {                       // no overlap at all — midpoint
        const mid = (w.t0 + w.t1) / 2
        const hit = list.find(({ clip }) => mid >= clip.in - 1e-3 && mid <= clip.out + 1e-3)
        if (hit) best = hit.index
      }
      if (best != null) owner.set(`${src}|${i}`, best)
    })
  }
  return owner
}

export function buildSegments(project, transcripts) {
  const clips = project?.clips || []
  const owner = ownerMap(project, transcripts)

  // Where a DELETED word is read. Source order decides which gap of the
  // FOOTAGE it fell into; it is then shown after that clip's take, wherever
  // that take ended up on the timeline. Words before the first clip of a
  // source are read before the first take that uses it.
  const srcOrder = new Map()    // src -> [{clip, index}] sorted by clip.in
  clips.forEach((c, index) => {
    const src = srcOf(c)
    if (!srcOrder.has(src)) srcOrder.set(src, [])
    srcOrder.get(src).push({ clip: c, index })
  })
  for (const list of srcOrder.values()) list.sort((a, b) => a.clip.in - b.clip.in)

  const leading = new Map()     // src -> [w] before that source's first clip
  const trailing = new Map()    // clip index -> [w] in the gap after it
  for (const [src, list] of srcOrder) {
    const words = transcripts?.[src]?.words || []
    words.forEach((w, i) => {
      if (owner.has(`${src}|${i}`)) return
      let k = -1
      for (let n = 0; n < list.length; n++) if (list[n].clip.in <= w.t0) k = n
      if (k < 0) {
        if (!leading.has(src)) leading.set(src, [])
        leading.get(src).push(w)
      } else {
        const idx = list[k].index
        if (!trailing.has(idx)) trailing.set(idx, [])
        trailing.get(idx).push(w)
      }
    })
  }

  const segments = []
  const usedSrc = new Set()     // sources whose leading words were emitted
  const notedSrc = new Set()    // sources that already showed "no transcript"
  clips.forEach((c, index) => {
    const src = srcOf(c)
    const tr = transcripts?.[src]
    const words = tr?.words || []
    const tStart = clipOutStart(project, index)
    const dur = c.out - c.in
    const entries = []

    if (!usedSrc.has(src)) {
      usedSrc.add(src)
      for (const w of leading.get(src) || []) entries.push({ type: 'word', kept: false, w, src })
    }
    entries.push({ type: 'take', index })

    // words[] is already in source-time order, and only this clip's are kept,
    // so the emitted kept words are non-decreasing in timelineT — which is
    // what makes the caller's binary search valid.
    words.forEach((w, i) => {
      if (owner.get(`${src}|${i}`) !== index) return
      // clamp INTO the clip: an opening word whose t0 whisper stamped before
      // the cut would otherwise get a timelineT earlier than the take itself,
      // breaking that same ordering.
      const off = Math.min(Math.max(w.t0 - c.in, 0), dur)
      entries.push({ type: 'word', kept: true, w, src, clipIndex: index, timelineT: tStart + off })
    })

    for (const w of trailing.get(index) || []) entries.push({ type: 'word', kept: false, w, src })

    segments.push({
      src, key: `${src}@${index}`, entries, clipIndex: index,
      hasTranscript: !!tr,
      // one note per source, not one per take — 40 clips of the same untranscribed
      // source would otherwise print the same line 40 times
      noteMissing: !tr && !notedSrc.has(src) && (notedSrc.add(src), true),
    })
  })
  return segments
}

// gap-midpoint between two source-time words (falls back to a small margin at
// the segment edge); used to place cut/split boundaries in the silence
export function gapMid(prevEnd, nextStart, margin) {
  if (prevEnd == null) return nextStart - margin
  if (nextStart == null) return prevEnd + margin
  return (prevEnd + nextStart) / 2
}
