import { useEffect, useMemo, useRef, useState } from 'react'
import { buildSegments, gapMid } from './txmodel'
import { fmtTime } from './time'
import { timeStore } from './timeStore'
import { api } from './api'

const LANGS = [['auto', 'Auto'], ['en', 'EN'], ['pt', 'PT'], ['de', 'DE'], ['es', 'ES']]

export default function Transcript({ s }) {
  const p = s.project
  const bodyRef = useRef(null)
  // OFF by default (REN-167): the panel is for reading what the video says
  // now, and struck-through words make that harder. Kept for the session so
  // turning it on to check a cut does not have to be redone on every project.
  const [showDeleted, setShowDeleted] = useState(
    () => sessionStorage.getItem('vs-show-deleted') === '1')
  const toggleDeleted = (on) => {
    setShowDeleted(on)
    try { sessionStorage.setItem('vs-show-deleted', on ? '1' : '0') } catch { /* private mode */ }
  }
  // Selection is a LIST of ranges over flat kept-word indices (REN-166). One
  // range is the ordinary drag; ⌘-click adds another, so several separate bits
  // of the video can be cut in a single action and undone in a single step.
  // Ranges may span takes and sources — doDelete regroups them per source.
  const [sel, setSel] = useState([])    // [{ a, b }]
  const [lang, setLang] = useState('auto')
  const [busy, setBusy] = useState(false)  // measuring cut boundaries (REN-164)
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

  // Decode the audio for cut boundaries while he reads (REN-164), so the first
  // delete answers immediately instead of waiting on ffmpeg. Best-effort.
  useEffect(() => {
    if (!s.currentId) return
    const srcs = [...new Set((p?.clips || []).map((c) => c.src || 'main'))]
    for (const src of srcs) api.cutWarm(s.currentId, src).catch(() => {})
  }, [s.currentId, p?.clips?.length]) // eslint-disable-line react-hooks/exhaustive-deps

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
  useEffect(() => { if (s.sel) setSel([]) }, [s.sel])

  // flat indices covered by the selection, in order
  const selIdx = useMemo(() => {
    const set = new Set()
    for (const r of sel) {
      for (let i = Math.min(r.a, r.b); i <= Math.max(r.a, r.b); i++) set.add(i)
    }
    return set
  }, [sel])

  // the run of words around `idx` that reads as one sentence. Bounded by
  // sentence-ending punctuation and by the take, so a triple click on a
  // transcript whisper punctuated poorly still selects something sane rather
  // than the whole video.
  const sentenceAt = (idx) => {
    const ends = (e) => /[.!?…]$/.test((e.w.w || '').trim())
    const clip = flatKept[idx]?.clipIndex
    let a = idx, b = idx
    while (a > 0 && flatKept[a - 1].clipIndex === clip && !ends(flatKept[a - 1])) a--
    while (b < flatKept.length - 1 && flatKept[b + 1].clipIndex === clip && !ends(flatKept[b])) b++
    return { a, b }
  }

  // that source's kept words in SOURCE order, which is what a cut boundary is
  // measured in. flatKept is in TIMELINE order (REN-165), and after an AI edit
  // the two differ.
  const keptOfSrc = (src) =>
    flatKept.filter((k) => k.src === src).sort((a, b) => a.w.t0 - b.w.t0)

  // Ask the server where a boundary really is, on the audio (REN-164). One
  // request per source; falls back to the old stamp midpoint if the measurement
  // is unavailable, so a cut is never blocked by it.
  const measure = async (src, gaps) => {
    if (!gaps.length) return []
    try {
      const r = await api.cutPoints(s.currentId, src, gaps)
      return r.points || []
    } catch { return [] }
  }
  const gapOf = (a, b, margin) => ({
    prev: a?.t1 ?? null, next: b?.t0 ?? null,
    prevStart: a?.t0 ?? null, nextEnd: b?.t1 ?? null, margin,
  })
  // worth telling him about: no pause exists AND the quietest instant available
  // is still within 6 dB of the speech around it, i.e. the cut has to go
  // through a vowel. Ordinary mid-sentence edits land ~15 dB below and are
  // inaudible, so they say nothing.
  const spliced = (pt) => !!pt && !pt.measured && (pt.level ?? -99) >= -6

  // Cut everything selected (any number of ranges, spanning takes and sources)
  // in ONE undo entry
  const doDelete = async () => {
    if (!selIdx.size || busy) return
    const chosen = flatKept.filter((e) => selIdx.has(e.flatIdx))
    if (!chosen.length) return

    // One cut span per CONTIGUOUS RUN of the footage, per source.
    //
    // The selection is a range on the TIMELINE, and after an AI edit the
    // timeline is not the order of the footage: two words side by side on
    // screen can sit seconds apart in the source, with words he did NOT select
    // in between. A single span from the first to the last would delete those
    // too. So the selection is regrouped against each source's own word order,
    // and only genuinely adjacent words become one span.
    const picked = new Map()
    for (const e of chosen) {
      if (!picked.has(e.src)) picked.set(e.src, new Set())
      picked.get(e.src).add(e)
    }
    const runs = []
    for (const [src, set] of picked) {
      const order = keptOfSrc(src)
      const hit = order.map((e, i) => (set.has(e) ? i : -1)).filter((i) => i >= 0)
      for (let n = 0; n < hit.length;) {
        let m = n
        while (m + 1 < hit.length && hit[m + 1] === hit[m] + 1) m++
        runs.push({ src, first: order[hit[n]], last: order[hit[m]],
                    prev: order[hit[n] - 1], next: order[hit[m] + 1] })
        n = m + 1
      }
    }
    if (!runs.length) return

    setBusy(true)
    const spans = [], rough = []
    try {
      for (const src of new Set(runs.map((r) => r.src))) {
        const mine = runs.filter((r) => r.src === src)
        const gaps = []
        for (const r of mine) {
          gaps.push(gapOf(r.prev?.w ?? null, r.first.w, 0.03))
          gaps.push(gapOf(r.last.w, r.next?.w ?? null, 0.05))
        }
        const pts = await measure(src, gaps)
        mine.forEach((r, i) => {
          const a = pts[2 * i], b = pts[2 * i + 1]
          spans.push({
            src,
            s0: a ? a.t : gapMid(r.prev?.w.t1 ?? null, r.first.w.t0, 0.03),
            s1: b ? b.t : gapMid(r.last.w.t1, r.next?.w.t0 ?? null, 0.05),
          })
          // No measured pause AND the cut has to go through audible sound: say
          // so. Deleting a word out of fluent speech always leaves some join,
          // so warning on every one of those would make the warning worthless —
          // `level` (dB over this room's noise floor) is what separates a real
          // splice through a vowel from an inaudible one between consonants.
          if (spliced(a) && r.prev) rough.push(`${r.prev.w.w} ${r.first.w.w}`)
          if (spliced(b) && r.next) rough.push(`${r.last.w.w} ${r.next.w.w}`)
        })
      }
    } finally { setBusy(false) }

    s.transcriptDelete(spans)
    setSel([])
    if (rough.length) {
      s.showToast(`Cut — but “${rough[0]}” run together in the audio${
        rough.length > 1 ? ` (+${rough.length - 1})` : ''}, so that join will sound spliced.`)
    }
  }

  // split the take at the end of the selection (isolate the range as a take)
  const doSplit = async () => {
    if (!selIdx.size || busy) return
    const at = flatKept[Math.max(...selIdx)]
    if (!at) return
    const order = keptOfSrc(at.src)
    const next = order[order.indexOf(at) + 1]
    if (!next) { s.showToast('Select a word to split after'); return }
    setBusy(true)
    let t = gapMid(at.w.t1, next.w.t0, 0.03)
    let measured = false
    try {
      const pts = await measure(at.src, [gapOf(at.w, next.w, 0.03)])
      if (pts[0]) { t = pts[0].t; measured = spliced(pts[0]) }
    } finally { setBusy(false) }
    s.transcriptSplitSource(at.src, t)
    setSel([])
    if (measured) s.showToast('Split — there is no pause there, so the two takes will sound joined.')
  }

  // keyboard: Delete cuts the range, Enter splits, Esc clears, Cmd/Ctrl+A all
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'TEXTAREA' || e.target.isContentEditable || e.target.tagName === 'INPUT') return
      if ((e.metaKey || e.ctrlKey) && (e.key === 'a' || e.key === 'A')) {
        if (!flatKept.length) return
        e.preventDefault(); e.stopPropagation()
        s.setSel(null); setSel([{ a: 0, b: flatKept.length - 1 }]); return
      }
      if (!selIdx.size) return
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); e.stopPropagation(); doDelete() }
      else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); doSplit() }
      else if (e.key === 'Escape') { e.stopPropagation(); setSel([]) }
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

  // Text-editor gestures (REN-166): drag selects, shift extends, ⌘-click adds a
  // separate piece, double-click takes the word, triple-click the sentence.
  //
  // The click count is counted here rather than read from `e.detail`: measured
  // in this app's own browser, a real `pointerdown` reports detail 0 (it is
  // mousedown that carries the count), so a triple click would never fire.
  const clickRef = useRef({ idx: -1, n: 0, t: 0 })
  const wordDown = (e, entry) => {
    if (!entry.kept) { s.transcriptRestore(entry.src, entry.w); setSel([]); return }
    s.setSel(null) // mutual exclusion with the timeline selection
    const i = entry.flatIdx
    const now = performance.now(), c = clickRef.current
    const clicks = (c.idx === i && now - c.t < 450) ? c.n + 1 : 1
    clickRef.current = { idx: i, n: clicks, t: now }
    if (clicks >= 3) {                         // triple — the whole sentence
      dragRef.current = null
      setSel((r) => (e.metaKey || e.ctrlKey ? [...r, sentenceAt(i)] : [sentenceAt(i)]))
    } else if (e.shiftKey && sel.length) {     // shift — stretch the last piece
      dragRef.current = sel[sel.length - 1].a
      setSel((r) => [...r.slice(0, -1), { a: r[r.length - 1].a, b: i }])
    } else if (e.metaKey || e.ctrlKey) {       // ⌘ — start a separate piece
      dragRef.current = i
      setSel((r) => [...r, { a: i, b: i }])
    } else {                                   // plain click (and double-click)
      dragRef.current = i
      setSel([{ a: i, b: i }])
    }
    s.setPlaying(false)
    s.setTime(entry.timelineT)
  }
  // dragging past a word grows whichever piece the drag started in
  const wordEnter = (entry) => {
    if (!entry.kept || dragRef.current == null) return
    const a = dragRef.current, b = entry.flatIdx
    setSel((r) => (r.length ? [...r.slice(0, -1), { a, b }] : [{ a, b }]))
  }
  const selHas = (entry) => entry.kept && selIdx.has(entry.flatIdx)
  const canSplit = selIdx.size > 0

  return (
    <div className="transcript">
      <div className="tx-toolbar">
        <label className="tx-toggle">
          <input type="checkbox" checked={showDeleted} onChange={(e) => toggleDeleted(e.target.checked)} />
          show deleted
        </label>
        <button className="tx-split" disabled={!canSplit || busy} onClick={doSplit}>✂ Split</button>
        <div className="spacer" />
        <span className="tx-hint">
          {busy ? 'listening to the audio…'
            : selIdx.size > 1 ? `${selIdx.size} words${sel.length > 1 ? ` in ${sel.length} pieces` : ''} · ⌫ cut`
            : 'drag · ⇧ extend · ⌘ add · 3× sentence · ⌫ cut'}
        </span>
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
                      title={entry.kept
                        ? 'drag to select · shift extends · ⌘ adds a piece · triple-click the sentence'
                        : 'click to restore'}
                      onPointerDown={(e) => wordDown(e, entry)}
                      onPointerOver={() => wordEnter(entry)}>
                  {entry.w.w}{' '}
                </span>
              )
            })}
            {sg.noteMissing && (
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
