import { useEffect, useMemo, useRef, useState } from 'react'
import { fileUrl } from './api'
import { capSpan, clipOutStart, fmtTime, materializeCap, outDuration, outToSource, trackFlag, typeLocked } from './time'
import { useTimeValue } from './timeStore'

// track lanes (REN-112): which controls each lane exposes + resize limits
const LANES = [
  { key: 'video', track: 'video', name: 'Video', mute: true, hide: true, min: 56, max: 220, def: 108 },
  { key: 'cap', track: 'captions', name: 'Captions', hide: true, min: 26, max: 48, def: 26 },
  { key: 'text', track: 'texts', name: 'Texts', hide: true, min: 26, max: 48, def: 26 },
  { key: 'ov', track: 'overlays', name: 'Overlays', mute: true, hide: true, min: 26, max: 48, def: 26 },
  { key: 'aud', track: 'audio', name: 'Audio', mute: true, min: 26, max: 72, def: 26 },
]
const DEFAULT_H = Object.fromEntries(LANES.map((l) => [l.key, l.def]))

// The playhead is the only thing that moves 60×/s — it subscribes to the clock
// alone so the rest of the timeline never re-renders during playback.
function Playhead({ pps, gutter }) {
  const time = useTimeValue()
  return (
    <div className="playhead" style={{ left: Math.round(time * pps) + gutter }}>
      <div className="cap" />
    </div>
  )
}

function SplitButton({ p, splitClip }) {
  const time = useTimeValue()
  const canSplit = (() => {
    if (!p.clips?.length) return false
    if (trackFlag(p, 'video', 'locked')) return false
    const [ci] = outToSource(p, time)
    if (ci == null) return false
    const clip = p.clips[ci]
    const start = clipOutStart(p, ci)
    const at = clip.in + (time - start)
    return at > clip.in + 0.2 && at < clip.out - 0.2
  })()
  return <button className="btn-split" onClick={splitClip} disabled={!canSplit}>✂ Split at playhead</button>
}

function TrackHead({ lane, p, s }) {
  const muted = trackFlag(p, lane.track, 'muted')
  const hidden = trackFlag(p, lane.track, 'hidden')
  const locked = trackFlag(p, lane.track, 'locked')
  const custom = p.tracks?.[lane.track]?.name
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const commit = () => { setEditing(false); s.renameTrack(lane.track, draft) }

  return (
    <div className={`tl-thead ${lane.key} ${locked ? 'locked' : ''} ${hidden ? 'hidden' : ''}`}>
      {editing ? (
        <input className="tl-tname edit" autoFocus value={draft} spellCheck={false}
               onChange={(e) => setDraft(e.target.value)}
               onBlur={commit}
               onKeyDown={(e) => {
                 if (e.key === 'Enter') commit()
                 if (e.key === 'Escape') setEditing(false)
                 e.stopPropagation()   // the timeline owns single-key shortcuts
               }} />
      ) : (
        <span className="tl-tname" title="Double-click to rename this track"
              onDoubleClick={() => { setDraft(custom || lane.name); setEditing(true) }}>
          {custom || lane.name}
        </span>
      )}
      <div className="tl-tctrl">
        {lane.mute && (
          <button className={`tl-tc ${muted ? 'on' : ''}`} title={muted ? 'Unmute track' : 'Mute track'}
                  onClick={() => s.toggleTrack(lane.track, 'muted')}>{muted ? '🔇' : '🔊'}</button>
        )}
        {lane.hide && (
          <button className={`tl-tc ${hidden ? 'on' : ''}`} title={hidden ? 'Show track' : 'Hide track'}
                  onClick={() => s.toggleTrack(lane.track, 'hidden')}>{hidden ? '🙈' : '👁'}</button>
        )}
        <button className={`tl-tc ${locked ? 'on' : ''}`} title={locked ? 'Unlock track' : 'Lock track'}
                onClick={() => s.toggleTrack(lane.track, 'locked')}>{locked ? '🔒' : '🔓'}</button>
      </div>
    </div>
  )
}


// Timeline filmstrip (REN-82, reworked). The old version painted the whole
// sprite as ONE background sized `totalSeconds * pxPerSec` wide, so zooming in
// stretched every frame sideways into a smear that told you nothing. CapCut
// keeps each thumbnail's shape and simply shows MORE of them — that is what this
// does: fixed-aspect cells, each showing the sprite frame nearest to its own
// moment in the clip.
function Filmstrip({ url, frames, interval, aspect, h, inSec, durSec, pps }) {
  const cellW = Math.max(10, Math.round(h * aspect))
  const n = Math.min(800, Math.max(1, Math.ceil((durSec * pps) / cellW)))
  const cells = []
  for (let j = 0; j < n; j++) {
    const t = inSec + ((j + 0.5) * cellW) / pps
    const k = Math.max(0, Math.min(frames - 1, Math.round(t / interval)))
    cells.push(
      <i key={j} style={{
        left: j * cellW, width: cellW,
        backgroundImage: `url("${url}")`,
        backgroundSize: `${frames * cellW}px ${h}px`,
        backgroundPositionX: `${-k * cellW}px`,
      }} />,
    )
  }
  return <div className="strip">{cells}</div>
}

// Clip notes (SCHEMA.md → clips.note). The cleanup pass keeps every good attempt
// of a line and labels them "take 2 of 4", so the owner can compare them instead
// of staring at N identical-looking clips.
const NOTE_RUN = /^take\s+(\d+)\s+of\s+(\d+)\b/i

// Which clips belong to a run of alternates. A rail is only drawn for a COMPLETE
// 1..M series — a partial or reordered one gets nothing, so the marker can never
// claim a grouping that isn't really there.
//
// One attempt can span SEVERAL clips: he says a sentence with a pause in it and
// the pass keeps that pause, so "take 2 of 3" legitimately repeats on each clip
// of that attempt. The series is therefore read over DISTINCT numbers, not over
// clips — reading it per clip drew a rail for 11 of 26 labelled clips.
function groupRails(clips) {
  const rails = new Array(clips.length).fill(null)
  let i = 0
  while (i < clips.length) {
    const m = NOTE_RUN.exec(clips[i].note || '')
    if (!m || +m[1] !== 1) { i++; continue }
    const total = +m[2]
    let j = i, want = 1
    while (j < clips.length) {
      const mj = NOTE_RUN.exec(clips[j].note || '')
      if (!mj || +mj[2] !== total) break
      const n = +mj[1]
      if (n === want) { j++; continue }          // same attempt, another clip
      if (n === want + 1) { want = n; j++; continue }   // next attempt
      break                                       // out of order — not a group
    }
    if (want === total && total >= 2 && j - i >= 2) {
      for (let k = i; k < j; k++) rails[k] = k === i ? 'start' : k === j - 1 ? 'end' : 'mid'
      i = j
    } else {
      i++
    }
  }
  return rails
}

// A note replaces the positional "take N" rather than sitting next to it — two
// different take numbers on one clip read as a contradiction.
//
// Fitting is measured, not guessed at a fixed pixel cutoff: at a fixed cutoff a
// 72px clip kept "take 1 of 3" and ellipsised it to "take…", which throws away
// the only half that matters. A note that does not fit shrinks to "1/3"; when
// even that would not fit it drops out entirely and the rail plus the hover
// tooltip carry the grouping.
const LAB_CH = 6.2   // JetBrains Mono advances 6.0px at the label's 10px; round up
                     // so the estimate can only ever be pessimistic — an
                     // over-optimistic one shows a note the CSS then ellipsises
const LAB_PAD = 34   // both trim handles + the pill's own padding
function clipLabel(note, i, w) {
  if (!note) return `take ${i + 1}`
  const room = w - LAB_PAD
  if (room >= note.length * LAB_CH) return note
  const m = NOTE_RUN.exec(note)
  if (m) {
    const short = `${m[1]}/${m[2]}`
    return room >= short.length * LAB_CH ? short : null
  }
  // free-form note: a few characters plus an ellipsis still says something
  return room >= 5 * LAB_CH ? note : null
}

export default function Timeline({ s }) {
  const { project: p, setTime, setPlaying, pxPerSec: pps, setPxPerSec,
          sel, setSel, mutate, beginGesture, splitClip, isMobile, busyMedia = [] } = s
  const rulerRef = useRef(null)
  const dur = useMemo(() => outDuration(p), [p])
  const tlWidth = Math.ceil(Math.max(dur, 1) * pps) + 120
  const GUTTER = isMobile ? 84 : 108

  // per-device track heights (localStorage, keyed by project) — a view
  // preference, NOT content, so it never enters undo history
  const [heights, setHeights] = useState(DEFAULT_H)
  useEffect(() => {
    try {
      const raw = localStorage.getItem(`vs-th-${s.currentId}`)
      setHeights(raw ? { ...DEFAULT_H, ...JSON.parse(raw) } : DEFAULT_H)
    } catch { setHeights(DEFAULT_H) }
  }, [s.currentId])
  const persistH = (h) => { try { localStorage.setItem(`vs-th-${s.currentId}`, JSON.stringify(h)) } catch { /* private mode */ } }

  function resizeStart(e, lane) {
    e.stopPropagation(); e.preventDefault()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
    const sy = e.clientY, h0 = heights[lane.key] || lane.def
    const move = (ev) => {
      const nh = Math.max(lane.min, Math.min(lane.max, Math.round(h0 + (ev.clientY - sy))))
      setHeights((h) => ({ ...h, [lane.key]: nh }))
    }
    const up = () => {
      setHeights((h) => { persistH(h); return h })
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }
  const resetHeight = (lane) => setHeights((h) => { const n = { ...h, [lane.key]: lane.def }; persistH(n); return n })

  const rails = useMemo(() => groupRails(p.clips || []), [p.clips])

  const ticks = []
  for (let i = 0; i <= Math.ceil(Math.max(dur, 1)); i++) ticks.push(i)

  function scrubStart(e) {
    e.preventDefault()
    const rect = rulerRef.current.getBoundingClientRect()
    const seek = (clientX) => setTime(Math.max(0, Math.min(dur, (clientX - rect.left) / pps)))
    setPlaying(false)
    seek(e.clientX)
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic/stale pointer */ }
    const move = (ev) => seek(ev.clientX)
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  function trimStart(e, clip, side) {
    if (trackFlag(p, 'video', 'locked')) return // locked track: no trim
    e.stopPropagation()
    e.preventDefault()
    setSel({ type: 'clip', id: clip.id })
    beginGesture()
    // captions are source-anchored now (REN-115) → trim needs no caption resync
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic/stale pointer */ }
    const sx = e.clientX
    const oin = clip.in, oout = clip.out
    const src = p.sources?.[clip.src || 'main']
    const srcDur = src?.duration ?? 1e9
    let moved = false
    const move = (ev) => {
      moved = true
      const dt = (ev.clientX - sx) / pps
      mutate((pp) => {
        const c = pp.clips.find((x) => x.id === clip.id)
        if (!c) return
        if (side === 'l') c.in = Math.max(0, Math.min(oout - 0.3, +(oin + dt).toFixed(2)))
        else c.out = Math.max(oin + 0.3, Math.min(srcDur, +(oout + dt).toFixed(2)))
        if (c.bg?.processed) c.bg.stale = true
        if (c.rt?.processed) c.rt.stale = true
      }, false)
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  const selectItem = (type, id) => {
    if (typeLocked(p, type)) return // locked track: not selectable (type 'clip' → 'video')
    setSel({ type, id })
  }

  // Move a caption, or stretch one of its edges (REN-157).
  //
  // `edge` null = drag the whole group, 'l'/'r' = one edge. The store converts
  // the timeline position back to source seconds and stops at the neighbours
  // rather than pushing them — nothing may ever overlap (REN-156). A magnet
  // pulls to nearby caption edges and clip cuts; alt suspends it.
  function capDrag(e, it, edge = null) {
    if (typeLocked(p, 'cap')) return
    e.stopPropagation(); e.preventDefault()
    setSel({ type: 'cap', id: it.id })
    beginGesture()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
    const sx = e.clientX
    const t0 = it.t0, t1 = it.t1
    // magnets: every other caption edge, every cut, and the playhead
    const snaps = []
    for (const o of elementLanes[0].items) {
      if (o.id === it.id) continue
      snaps.push(o.t0, o.t1)
    }
    let acc = 0
    for (const c of p.clips || []) { snaps.push(acc); acc += c.out - c.in }
    snaps.push(acc)
    const SNAP_PX = 7
    const move = (ev) => {
      let t = (edge === 'r' ? t1 : t0) + (ev.clientX - sx) / pps
      if (!ev.altKey) {
        let best = null, bd = SNAP_PX / pps
        for (const sp of snaps) { const d = Math.abs(sp - t); if (d < bd) { bd = d; best = sp } }
        if (best != null) t = best
      }
      s.moveCaption(it.id, +t.toFixed(3), edge)
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  // drag the volume line over a clip's waveform (CapCut-style) — vol 0–2, one undo
  function volDrag(e, clip) {
    if (trackFlag(p, 'video', 'locked')) return
    e.stopPropagation(); e.preventDefault()
    setSel({ type: 'clip', id: clip.id })
    beginGesture()
    const band = e.currentTarget.parentElement.getBoundingClientRect()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
    const apply = (clientY) => {
      const frac = Math.max(0, Math.min(1, (clientY - band.top) / band.height))
      const vol = +(2 * (1 - frac)).toFixed(2)
      mutate((pp) => { const c = pp.clips.find((x) => x.id === clip.id); if (c) c.vol = vol }, false)
    }
    apply(e.clientY)
    const move = (ev) => apply(ev.clientY)
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  const elementLanes = [
    { key: 'cap', items: (p.captions || []).map((c) => {
      const mat = materializeCap(p, c) // source-anchored → derive timeline span
      if (!mat) return null            // fully cut: not on the timeline
      const span = capSpan(mat) || [0, 1]
      return { id: c.id, t0: span[0], t1: span[1] - 0.15, label: (c.words || []).map((w) => w.w).join(' ') }
    }).filter(Boolean) },
    { key: 'text', items: (p.texts || []).map((x) => ({
      id: x.id, t0: x.t0, t1: x.t1, label: (x.text || '').replace(/\n/g, ' ') })) },
    { key: 'ov', items: (p.overlays || []).map((o) => ({
      id: o.id, t0: o.t0, t1: o.t1 ?? dur, label: o.name, raw: o })) },
    { key: 'aud', items: (p.audios || []).map((a) => ({
      id: a.id, t0: a.t0, t1: a.t1 ?? dur, label: `♪ ${a.name}`, raw: a })) },
  ]
  const laneOf = (key) => LANES.find((l) => l.key === key)

  return (
    <div className="timeline" style={{ height: isMobile ? 264 : 290 }}>
      <div className="tl-toolbar">
        <SplitButton p={p} splitClip={splitClip} />
        <div className="tl-counter">{(p.clips || []).length} clips · {fmtTime(dur)}</div>
        <div className="spacer" />
        <div className="tl-zoom">
          Zoom
          <input type="range" min="12" max="140" step="1" value={pps}
                 onChange={(e) => setPxPerSec(+e.target.value)} />
        </div>
      </div>
      <div className="tl-scroll">
        <div className="tl-inner" style={{ width: GUTTER + tlWidth, ['--gutter']: `${GUTTER}px` }}>
          {/* ruler row */}
          <div className="tl-row tl-rulerrow">
            <div className="tl-thead tl-rulerhead" />
            <div className="tl-ruler" ref={rulerRef} onPointerDown={scrubStart} style={{ width: tlWidth }}>
              {ticks.map((i) => (
                <div key={i} className="tl-tick" style={{ left: Math.round(i * pps) }}>
                  <div className="mark" />
                  <span className="lab">{fmtTime(i)}</span>
                </div>
              ))}
            </div>
          </div>

          {/* video lane */}
          <div className="tl-row" style={{ height: heights.video }}>
            <TrackHead lane={laneOf('video')} p={p} s={s} />
            <div className={`tl-lanebody videolane ${trackFlag(p, 'video', 'locked') ? 'locked' : ''} ${trackFlag(p, 'video', 'hidden') ? 'hidden' : ''}`}
                 style={{ width: tlWidth }}
                 onPointerDown={(e) => { if (e.target === e.currentTarget) setSel(null) }}>
              {(p.clips || []).map((c, i) => {
                const isSel = sel?.type === 'clip' && sel.id === c.id
                const src = p.sources?.[c.src || 'main']
                const hasStrip = !!src?.strip
                const locked = trackFlag(p, 'video', 'locked')
                const w = Math.max(24, Math.round((c.out - c.in) * pps))
                const note = typeof c.note === 'string' ? c.note.trim() : ''
                const label = clipLabel(note, i, w)
                return (
                  <div key={c.id}
                       className={`tl-clip ${c.bg ? 'hasbg' : ''} ${isSel ? 'sel' : ''}`}
                       style={{ left: Math.round(clipOutStart(p, i) * pps), height: heights.video, width: w }}
                       title={note || undefined}
                       onPointerDown={(e) => { e.stopPropagation(); selectItem('clip', c.id) }}>
                    <div className="frames-band">
                      {hasStrip ? (
                        <>
                          <Filmstrip url={fileUrl(p, src.strip)}
                                     frames={src.stripFrames} interval={src.stripInterval || 1}
                                     aspect={(p.w || 1080) / (p.h || 1920)} h={heights.video}
                                     inSec={c.in} durSec={c.out - c.in} pps={pps} />
                          <div className="scrim" />
                          {c.bg && <div className="tint-bg" />}
                        </>
                      ) : (
                        <div className="stripes" />
                      )}
                    </div>
                    {/* dedicated waveform band under the frames (REN-114/CapCut) —
                        same width/left math as the strip; per-clip volume line */}
                    <div className="wave-band">
                      {src?.vwave && (
                        <div className="vwave" style={{
                          backgroundImage: `url("${fileUrl(p, src.vwave)}")`,
                          backgroundSize: `${(src.duration || (c.out - c.in)) * pps}px 100%`,
                          backgroundPositionX: `${-c.in * pps}px`,
                        }} />
                      )}
                      {!locked && (
                        <div className="vol-line" style={{ top: `${(1 - (c.vol ?? 1) / 2) * 100}%` }}
                             title={`clip volume · drag to change`}
                             onPointerDown={(e) => volDrag(e, c)}>
                          <span className="vol-tag">{Math.round((c.vol ?? 1) * 100)}%</span>
                        </div>
                      )}
                    </div>
                    {rails[i] && <div className={`grp ${rails[i]}`} />}
                    {(label != null || c.bg || c.rt) && (
                      <div className={`lab ${hasStrip ? 'pill' : ''} ${note ? 'noted' : ''}`}>
                        {label != null && <span className="lab-txt">{label}</span>}
                        {c.bg && <span className="chip-bg">BG</span>}
                        {c.rt && <span className="chip-rt">RT</span>}
                      </div>
                    )}
                    {busyMedia.includes(c.id) && <div className="tl-uploading">processing…</div>}
                    {!locked && <div className="trim-handle l" onPointerDown={(e) => trimStart(e, c, 'l')} />}
                    {!locked && <div className="trim-handle r" onPointerDown={(e) => trimStart(e, c, 'r')} />}
                  </div>
                )
              })}
            </div>
            <div className="tl-resize" title="Drag to resize this track · double-click to reset"
                 onPointerDown={(e) => resizeStart(e, laneOf('video'))}
                 onDoubleClick={() => resetHeight(laneOf('video'))} />
          </div>

          {/* element lanes */}
          {elementLanes.map((lane) => {
            const meta = laneOf(lane.key)
            const locked = trackFlag(p, meta.track, 'locked')
            const hidden = trackFlag(p, meta.track, 'hidden')
            const h = heights[lane.key]
            return (
              <div key={lane.key} className="tl-row" style={{ height: h }}>
                <TrackHead lane={meta} p={p} s={s} />
                <div className={`tl-lanebody ${locked ? 'locked' : ''} ${hidden ? 'hidden' : ''}`}
                     style={{ width: tlWidth }}
                     onPointerDown={(e) => { if (e.target === e.currentTarget) setSel(null) }}>
                  {lane.items.map((it) => {
                    const o = it.raw
                    let media = null
                    if (lane.key === 'ov' && o) {
                      if (o.kind === 'image') {
                        media = { backgroundImage: `url("${fileUrl(p, o.cut && o.cutPath ? o.cutPath : o.path)}")`,
                                  backgroundSize: 'auto 100%', backgroundRepeat: 'repeat-x' }
                      } else if (o.strip) {
                        // same rule as the clip strip: a frame keeps its shape and
                        // the row repeats, instead of one frame smearing wider as
                        // you zoom
                        const cw = Math.max(10, Math.round(heights.overlays * 0.6 * ((p.w || 1080) / (p.h || 1920))))
                        media = { backgroundImage: `url("${fileUrl(p, o.strip)}")`,
                                  backgroundSize: `${o.stripFrames * cw}px 100%`,
                                  backgroundRepeat: 'repeat-x' }
                      }
                    }
                    if (lane.key === 'aud' && o?.wave) {
                      media = { backgroundImage: `url("${fileUrl(p, o.wave)}")`,
                                backgroundSize: `${(o.dur || (it.t1 - it.t0)) * pps}px 100%`,
                                backgroundRepeat: 'no-repeat' }
                    }
                    return (
                      <div key={it.id}
                           className={`tl-block ${lane.key} ${sel?.type === lane.key && sel.id === it.id ? 'sel' : ''}`}
                           style={{ left: Math.round(it.t0 * pps), height: Math.max(18, h - 4), width: Math.max(20, Math.round((it.t1 - it.t0) * pps)) }}
                           title={lane.key === 'cap'
                             ? `${it.label}\ndrag to move · edges to stretch · alt disables the magnet`
                             : it.label}
                           onPointerDown={(e) => {
                             e.stopPropagation()
                             selectItem(lane.key, it.id)
                             // captions move on the timeline (REN-157); the other
                             // lanes keep their inspector-only behaviour
                             if (lane.key === 'cap' && !locked) capDrag(e, it, null)
                           }}>
                        {media && <div className="bgimg" style={media} />}
                        <span className={media ? 'pill' : ''}>{it.label}</span>
                        {lane.key === 'cap' && !locked && (
                          <>
                            <div className="trim-handle l" onPointerDown={(e) => capDrag(e, it, 'l')} />
                            <div className="trim-handle r" onPointerDown={(e) => capDrag(e, it, 'r')} />
                          </>
                        )}
                        {busyMedia.includes(it.id) && <div className="tl-uploading">processing…</div>}
                      </div>
                    )
                  })}
                </div>
                <div className="tl-resize" title="Drag to resize this track · double-click to reset"
                     onPointerDown={(e) => resizeStart(e, meta)}
                     onDoubleClick={() => resetHeight(meta)} />
              </div>
            )
          })}

          <Playhead pps={pps} gutter={GUTTER} />
        </div>
      </div>
    </div>
  )
}
