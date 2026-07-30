import { useEffect, useMemo, useRef, useState } from 'react'
import { buildSegments, gapMid } from './txmodel'
import { fmtTime } from './time'
import { timeStore } from './timeStore'

const LANGS = [['auto', 'Auto'], ['en', 'EN'], ['pt', 'PT'], ['de', 'DE'], ['es', 'ES']]

export default function Transcript({ s }) {
  const p = s.project
  const bodyRef = useRef(null)
  const [showDeleted, setShowDeleted] = useState(true)
  const [sel, setSel] = useState(null) // { a, b } — flat kept-word index range (can span takes)
  const [lang, setLang] = useState('auto')
  const dragRef = useRef(null)          // flat index anchoring an in-progress drag

  const segments = useMemo(() => buildSegments(p, s.transcripts), [p, s.transcripts])
  const anyTranscript = segments.some((sg) => sg.hasTranscript)
  const missing = segments.some((sg) => !sg.hasTranscript)

  // flat kept words in timeline order; annotate each with its flat index so
  // selection ranges (REN-113) can span segments/takes
  const flatKept = useMemo(() => {
    const arr = segments.flatMap((sg) => sg.entries.filter((e) => e.type === 'word' && e.kept))
    arr.forEach((e, i) => { e.flatIdx = i })
    return arr
  }, [segments])

  // load transcripts already on disk when the panel opens / project changes
  useEffect(() => { s.loadTranscripts() }, [p?.sources, s.transcripts]) // eslint-disable-line react-hooks/exhaustive-deps

  // highlight + auto-scroll the currently-spoken word without re-rendering 60×/s
  useEffect(() => {
    let lastIdx = -1
    const apply = () => {
      const t = timeStore.get()
      let lo = 0, hi = flatKept.length - 1, idx = -1
      while (lo <= hi) { const m = (lo + hi) >> 1; if (flatKept[m].timelineT <= t + 1e-3) { idx = m; lo = m + 1 } else hi = m - 1 }
      if (idx === lastIdx) return
      if (lastIdx >= 0) document.getElementById(`tw-${lastIdx}`)?.classList.remove('now')
      if (idx >= 0) {
        const el = document.getElementById(`tw-${idx}`)
        if (el) { el.classList.add('now'); if (s.playing) el.scrollIntoView({ block: 'nearest' }) }
      }
      lastIdx = idx
    }
    const unsub = timeStore.subscribe(apply)
    apply()
    return unsub
  }, [flatKept, s.playing])

  // the transcript word selection and the timeline element selection both react
  // to Delete — keep them mutually exclusive so each key goes to the right one
  useEffect(() => { if (s.sel) setSel(null) }, [s.sel])

  // Cut the selected range (may span takes/sources) in one undo entry
  const doDelete = () => {
    if (!sel) return
    const lo = Math.min(sel.a, sel.b), hi = Math.max(sel.a, sel.b)
    const chosen = flatKept.slice(lo, hi + 1)
    if (!chosen.length) return
    // one cut span per source, using that source's own kept-word neighbors so the
    // boundaries land in the silence (lead-in 0.03s, tail 0.05s)
    const spans = []
    const seen = new Set()
    for (const entry of chosen) {
      if (seen.has(entry.src)) continue
      seen.add(entry.src)
      const srcKept = flatKept.filter((k) => k.src === entry.src)
      const ofSrc = chosen.filter((k) => k.src === entry.src)
      const first = ofSrc[0], last = ofSrc[ofSrc.length - 1]
      const fi = srcKept.indexOf(first), li = srcKept.indexOf(last)
      const prev = srcKept[fi - 1], next = srcKept[li + 1]
      spans.push({
        src: entry.src,
        s0: gapMid(prev?.w.t1 ?? null, first.w.t0, 0.03),
        s1: gapMid(last.w.t1, next?.w.t0 ?? null, 0.05),
      })
    }
    s.transcriptDelete(spans)
    setSel(null)
  }

  // split the take at the end of the selection (isolate the range as a take)
  const doSplit = () => {
    if (!sel) return
    const at = flatKept[Math.max(sel.a, sel.b)]
    if (!at) return
    const srcKept = flatKept.filter((k) => k.src === at.src)
    const next = srcKept[srcKept.indexOf(at) + 1]
    if (!next) { s.showToast('Select a word to split after'); return }
    s.transcriptSplitSource(at.src, gapMid(at.w.t1, next.w.t0, 0.03))
    setSel(null)
  }

  // keyboard: Delete cuts the range, Enter splits, Esc clears, Cmd/Ctrl+A all
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'TEXTAREA' || e.target.isContentEditable || e.target.tagName === 'INPUT') return
      if ((e.metaKey || e.ctrlKey) && (e.key === 'a' || e.key === 'A')) {
        if (!flatKept.length) return
        e.preventDefault(); e.stopPropagation()
        s.setSel(null); setSel({ a: 0, b: flatKept.length - 1 }); return
      }
      if (!sel) return
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); e.stopPropagation(); doDelete() }
      else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); doSplit() }
      else if (e.key === 'Escape') { e.stopPropagation(); setSel(null) }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }) // re-bind each render so the handlers close over the latest sel

  // end an in-progress drag anywhere
  useEffect(() => {
    const up = () => { dragRef.current = null }
    window.addEventListener('pointerup', up)
    return () => window.removeEventListener('pointerup', up)
  }, [])

  const wordDown = (e, entry) => {
    if (!entry.kept) { s.transcriptRestore(entry.src, entry.w); setSel(null); return }
    s.setSel(null) // mutual exclusion with the timeline selection
    if (e.shiftKey && sel) setSel({ a: sel.a, b: entry.flatIdx })
    else { setSel({ a: entry.flatIdx, b: entry.flatIdx }); dragRef.current = entry.flatIdx }
    s.setPlaying(false)
    s.setTime(entry.timelineT)
  }
  const wordEnter = (entry) => {
    if (entry.kept && dragRef.current != null) setSel({ a: dragRef.current, b: entry.flatIdx })
  }
  const wordDouble = (entry) => {
    if (entry.kept) { s.setTime(entry.timelineT); s.setPlaying(true) }
  }

  const selHas = (entry) => entry.kept && sel &&
    entry.flatIdx >= Math.min(sel.a, sel.b) && entry.flatIdx <= Math.max(sel.a, sel.b)
  const canSplit = !!sel

  return (
    <div className="transcript">
      <div className="tx-toolbar">
        <label className="tx-toggle">
          <input type="checkbox" checked={showDeleted} onChange={(e) => setShowDeleted(e.target.checked)} />
          show deleted
        </label>
        <button className="tx-split" disabled={!canSplit} onClick={doSplit}>✂ Split</button>
        <div className="spacer" />
        <span className="tx-hint">drag to select · ⌫ cut · ⏎ split · ⌘A all</span>
      </div>

      <div className="tx-body" ref={bodyRef}>
        {!anyTranscript && (
          <div className="tx-empty">
            <p>Edit by transcript, Descript-style: delete words to cut the video, split takes by text.</p>
            <div className="tx-lang">
              {LANGS.map(([code, label]) => (
                <button key={code} className={lang === code ? 'on' : ''} onClick={() => setLang(code)}>{label}</button>
              ))}
            </div>
            <button className="tx-transcribe" disabled={!!s.transcribing.length}
                    onClick={() => s.transcribeSources(lang)}>
              {s.transcribing.length ? 'Transcribing takes…' : 'Transcribe takes'}
            </button>
          </div>
        )}

        {segments.map((sg) => (
          <div key={sg.key} className="tx-seg">
            {sg.entries.map((entry, i) => {
              if (entry.type === 'take') {
                const c = p.clips[entry.index]
                return (
                  <div key={`take-${i}`} className="tx-take">take {entry.index + 1} · {fmtTime(c ? clipStart(p, entry.index) : 0)}</div>
                )
              }
              if (!entry.kept && !showDeleted) return null
              return (
                <span key={`w-${i}`} id={entry.kept ? `tw-${entry.flatIdx}` : undefined}
                      className={`tx-word ${entry.kept ? 'kept' : 'deleted'} ${selHas(entry) ? 'sel' : ''}`}
                      title={entry.kept ? 'drag to select · click seek · double-click play' : 'click to restore'}
                      onPointerDown={(e) => wordDown(e, entry)}
                      onPointerOver={() => wordEnter(entry)}
                      onDoubleClick={() => wordDouble(entry)}>
                  {entry.w.w}{' '}
                </span>
              )
            })}
            {!sg.hasTranscript && sg.entries.length > 0 && (
              <div className="tx-nosrc">{s.transcribing.includes(sg.src) ? 'transcribing…' : 'no transcript for this take yet'}</div>
            )}
          </div>
        ))}

        {anyTranscript && missing && (
          <button className="tx-transcribe small" disabled={!!s.transcribing.length}
                  onClick={() => s.transcribeSources(lang)}>
            {s.transcribing.length ? 'Transcribing…' : 'Transcribe remaining takes'}
          </button>
        )}
      </div>
    </div>
  )
}

// local clip-start helper (avoids importing clipOutStart everywhere)
function clipStart(project, idx) {
  return (project.clips || []).slice(0, idx).reduce((a, c) => a + (c.out - c.in), 0)
}
