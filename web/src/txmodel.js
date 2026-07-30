// Transcript view model (REN-83): fold the project's clips + per-source
// transcripts into a Descript-style reading order. A word is KEPT when its
// source moment is covered by a clip, DELETED (struck) otherwise. Take headers
// mark where each clip's coverage begins. Nothing here is stored — it's derived
// from clips + transcripts so it can never desync.
import { clipOutStart } from './time'

export function buildSegments(project, transcripts) {
  const clips = project.clips || []
  const segments = []
  // keptIndex must be UNIQUE per source across ALL its runs — a source can
  // appear in two non-adjacent runs (split then a different take inserted
  // between), and Transcript resolves selections against the union per source.
  const srcKept = {}
  let i = 0
  while (i < clips.length) {
    const src = clips[i].src || 'main'
    let j = i
    while (j < clips.length && (clips[j].src || 'main') === src) j++
    const segClips = []
    for (let k = i; k < j; k++) segClips.push({ clip: clips[k], index: k, tStart: clipOutStart(project, k) })
    const bySrc = segClips.slice().sort((a, b) => a.clip.in - b.clip.in)
    const tr = transcripts[src]
    const words = tr?.words || []
    const entries = []
    let curClip = null
    for (const w of words) {
      const mid = (w.t0 + w.t1) / 2
      const cov = bySrc.find((sc) => mid >= sc.clip.in - 1e-3 && mid <= sc.clip.out + 1e-3)
      if (cov) {
        if (cov.index !== curClip) { entries.push({ type: 'take', index: cov.index }); curClip = cov.index }
        const ki = (srcKept[src] = (srcKept[src] ?? 0))
        srcKept[src]++
        entries.push({ type: 'word', kept: true, w, src,
          clipIndex: cov.index, keptIndex: ki,
          timelineT: cov.tStart + (w.t0 - cov.clip.in) })
      } else {
        entries.push({ type: 'word', kept: false, w, src })
      }
    }
    if (!words.length) for (const sc of bySrc) entries.push({ type: 'take', index: sc.index, empty: true })
    segments.push({ src, key: `${src}@${i}`, entries, hasTranscript: !!tr })
    i = j
  }
  return segments
}

// gap-midpoint between two source-time words (falls back to a small margin at
// the segment edge); used to place cut/split boundaries in the silence
export function gapMid(prevEnd, nextStart, margin) {
  if (prevEnd == null) return nextStart - margin
  if (nextStart == null) return prevEnd + margin
  return (prevEnd + nextStart) / 2
}
