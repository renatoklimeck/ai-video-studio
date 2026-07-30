import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import { FONT_NAMES, fmtTime, mkWords, mkWordsSrcCovered, typeLocked } from './time'

function NumField({ label, value, step = 0.1, min, max, onChange, onFocus, className = '' }) {
  const [draft, setDraft] = useState(null)
  return (
    <label className={`field ${className}`}>
      {label}
      <input type="number" step={step} min={min} max={max}
             value={draft ?? (value ?? 0)}
             onFocus={onFocus}
             onChange={(e) => {
               setDraft(e.target.value)
               const v = parseFloat(e.target.value)
               if (Number.isFinite(v)) onChange(v)
             }}
             onBlur={() => setDraft(null)}
             onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur() }} />
    </label>
  )
}

// maxW clamps to 20–96 — with a plain controlled input the clamp rewrites the
// field on every keystroke ("3" of "35" → 20), so keep a draft while focused
// and apply the clamped value live underneath
function MaxWField({ value, onLive, onFocus }) {
  const [draft, setDraft] = useState(null)
  return (
    <label className="field span2">Max width (% — empty = auto)
      <input type="text" placeholder="auto" value={draft ?? (value ?? '')}
             onFocus={onFocus}
             onChange={(e) => {
               setDraft(e.target.value)
               const v = parseFloat(e.target.value)
               onLive(Number.isFinite(v) ? Math.max(20, Math.min(96, v)) : null)
             }}
             onBlur={() => setDraft(null)}
             onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur() }} />
    </label>
  )
}

const RT_NATURAL = { preset: 'natural', intensity: 35, smooth: 30, even: 25, blem: 20, shine: 15, plump: 10, eyes: 20, circles: 10, scope: 'clip' }
const RT_STUDIO = { preset: 'studio', intensity: 60, smooth: 55, even: 45, blem: 40, shine: 30, plump: 25, eyes: 35, circles: 25, scope: 'clip' }
// On activate everything is zeroed (Renato's ask) EXCEPT Intensity, which is the
// master scale — 50 = neutral so moving any fine-tune from 0 shows immediately.
// Nothing changes on screen until a fine-tune leaves 0. ↺ resets to this.
const RT_ZERO = { preset: 'custom', intensity: 50, smooth: 0, even: 0, blem: 0, shine: 0, plump: 0, eyes: 0, circles: 0, scope: 'clip' }
const rtSig = (rt) => ['intensity', 'smooth', 'even', 'blem', 'shine', 'plump', 'eyes', 'circles'].map((k) => rt?.[k] ?? 0).join(',')
const RT_SLIDERS = [
  ['smooth', 'Smooth skin'], ['even', 'Even tone'], ['blem', 'Clear blemishes'],
  ['shine', 'De-shine'], ['plump', 'Plump'], ['eyes', 'Brighten eyes'], ['circles', 'Dark circles'],
]

export default function Inspector({ s }) {
  const { project: p, sel, setSel, mutate, beginGesture, deleteSel, isMobile,
          bgJob, bgJobPct, processBg, currentId,
          rtJob, rtJobPct, processRt, faceTracks, detectFace } = s
  const bgImgRef = useRef(null)
  const capSpanRef = useRef(null)
  const [capDraft, setCapDraft] = useState(null)
  const [cutBusy, setCutBusy] = useState(false)

  const listMap = { clip: 'clips', cap: 'captions', text: 'texts', ov: 'overlays', aud: 'audios' }
  const titles = { clip: 'Clip', cap: 'Caption', text: 'Headline', ov: 'Overlay', aud: 'Audio' }
  const item = sel ? (p[listMap[sel.type]] || []).find((x) => x.id === sel.id) : null
  const hasSel = !!(sel && item)

  const edit = (fn, undoable = true) => {
    if (typeLocked(p, sel.type)) return // locked track: read-only in the inspector
    mutate((pp) => {
      const it = (pp[listMap[sel.type]] || []).find((x) => x.id === sel.id)
      if (it) fn(it, pp)
    }, undoable)
  }
  // continuous controls (sliders, number spinners, textareas, color pickers)
  // push ONE undo entry per editing session: beginGesture on focus/pointerdown,
  // then non-undoable live edits
  const editLive = (fn) => edit(fn, false)

  // editing a processed clip (in/out) or its bg (color/feather/image) marks it stale
  const editClipStale = (fn, undoable = true) => edit((c, pp) => {
    fn(c, pp)
    if (c.bg?.processed) c.bg.stale = true
    // a bg param change also invalidates the retouch preview (it composites bg)
    if (c.rt?.processed) c.rt.stale = true
  }, undoable)
  const editClipStaleLive = (fn) => editClipStale(fn, false)

  async function toggleCut() {
    const o = item
    if (o.cut) { edit((x) => { x.cut = false }); return }
    if (o.cutPath) { edit((x) => { x.cut = true }); return }
    setCutBusy(true)
    try {
      const { path } = await api.cutout(currentId, o.path)
      edit((x) => { x.cut = true; x.cutPath = path })
    } finally { setCutBusy(false) }
  }

  async function pickBgImage(e) {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    const { path } = await api.upload(currentId, f)
    editClipStale((c) => { if (c.bg) c.bg.image = path })
  }

  const rt = hasSel && sel.type === 'clip' ? item.rt : null
  const rtStrong = rt && RT_SLIDERS.some(([k]) => rt[k] > 40)
  const setRt = (field) => (e) => {
    const v = parseInt(e.target.value, 10) || 0
    editLive((c) => {
      if (!c.rt) return
      c.rt[field] = v
      c.rt.preset = 'custom'
      if (c.rt.processed) c.rt.stale = true // tuning invalidates the processed preview
    })
  }
  // real face detection for the honest chip + live mask (runtime only)
  const rtSrc = rt ? (item.src || 'main') : null
  useEffect(() => {
    if (rtSrc) detectFace(rtSrc)
  }, [rtSrc, detectFace])
  const track = rtSrc ? faceTracks[rtSrc] : null
  const faceInClip = track && (track.samples || []).some((sp) => sp.t >= item.in && sp.t <= item.out)
  const rtFresh = rt && rt.processed && !rt.stale && rt._cache &&
    rt._cache.sig === rtSig(rt) && rt._cache.in === item.in && rt._cache.out === item.out

  // caption audio span — source seconds for source-anchored groups (REN-115),
  // timeline seconds for legacy
  const capSrc = hasSel && sel.type === 'cap' && item.capAnchor === 'source'
  const capT0 = hasSel && sel.type === 'cap' && item.words.length
    ? (capSrc ? item.words[0].t0 : item.words[0].t) : 0
  const capT1 = hasSel && sel.type === 'cap' && item.words.length
    ? (capSrc ? item.words[item.words.length - 1].t1
      : item.words[item.words.length - 1].t + item.words[item.words.length - 1].d) : 0

  return (
    <div className={`inspector ${isMobile ? 'sheet' : ''}`}>
      <div className="insp-header">
        <div className="insp-title">{hasSel ? titles[sel.type] : 'Nothing selected'}</div>
        <div className="right">
          {hasSel && <button className="btn-delete" title="Delete (⌫)" onClick={deleteSel}>Delete</button>}
          {isMobile && <span className="insp-x" onClick={() => setSel(null)}>✕</span>}
        </div>
      </div>
      <div className="insp-body">

        {!hasSel && (
          <div className="insp-none">
            <div className="tile">◇</div>
            <p>Select a clip or element in the timeline — or drag things directly on the preview.</p>
          </div>
        )}

        {/* ── CLIP ── */}
        {hasSel && sel.type === 'clip' && (
          <>
            <div className="grid2">
              <NumField label="Start (source, s)" value={item.in} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editClipStaleLive((c) => { c.in = Math.max(0, Math.min(c.out - 0.3, v)) })} />
              <NumField label="End (source, s)" value={item.out} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editClipStaleLive((c) => { c.out = Math.max(c.in + 0.3, v) })} />
              <NumField label="Fade in (s)" value={item.fadeIn} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editLive((c) => { c.fadeIn = Math.max(0, v) })} />
              <NumField label="Fade out (s)" value={item.fadeOut} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editLive((c) => { c.fadeOut = Math.max(0, v) })} />
            </div>
            <label className="slider-label">Clip volume · {Math.round((item.vol ?? 1) * 100)}%
              <input type="range" min="0" max="2" step="0.05" value={item.vol ?? 1}
                     onPointerDown={beginGesture}
                     onChange={(e) => editLive((c) => { c.vol = +e.target.value })} />
            </label>

            <div className="insp-stack">
              <div className="insp-row-between">
                <div className="insp-section-label">Zoom · keyframes</div>
                <button className="btn-violet mini" onClick={() => edit((c) => {
                  const d = c.out - c.in
                  c.kfs = [{ t: 0, scale: 1, cx: 50, cy: 50 }, { t: +d.toFixed(1), scale: 1.15, cx: 50, cy: 45 }]
                })}>+ Punch-in 1.15×</button>
              </div>
              {(item.kfs || []).map((k, i) => (
                <div key={i} className="kf-row">
                  <span className="diamond">◆</span>
                  <span>t={k.t}s</span>
                  <span>×{k.scale}</span>
                  <span className="c">c {k.cx},{k.cy}</span>
                  <span className="x" onClick={() => edit((c) => c.kfs.splice(i, 1))}>✕</span>
                </div>
              ))}
              {!(item.kfs || []).length && <div className="insp-hint">No keyframes — this clip stays static.</div>}
            </div>

            {/* Background removal card */}
            <div className="insp-card">
              <label className="check">
                <input type="checkbox" checked={!!item.bg}
                       onChange={(e) => edit((c) => {
                         c.bg = e.target.checked
                           ? { color: '#0E3B34', feather: 4, image: null, processed: false, stale: false }
                           : null
                       })} />
                Remove background on this clip
              </label>
              {item.bg && (
                <>
                  <div className="grid2">
                    <label className="field">Background color
                      <input type="color" value={item.bg.color || '#0E3B34'} onFocus={beginGesture}
                             onChange={(e) => editClipStaleLive((c) => { c.bg.color = e.target.value })} />
                    </label>
                    <NumField label="Edge feather (px)" value={item.bg.feather} step={1} onFocus={beginGesture}
                              onChange={(v) => editClipStaleLive((c) => { c.bg.feather = Math.max(0, v) })} />
                  </div>
                  <button className="btn-dashed" onClick={() => bgImgRef.current?.click()}>
                    {item.bg.image ? `Background image: ${item.bg.image.split('/').pop()}` : 'Background image… (upload)'}
                  </button>
                  <input ref={bgImgRef} type="file" accept="image/*" hidden onChange={pickBgImage} />
                  {item.bg.image && (
                    <div className="insp-hint" style={{ cursor: 'pointer' }}
                         onClick={() => editClipStale((c) => { c.bg.image = null })}>✕ remove background image</div>
                  )}
                  {bgJob === item.id ? (
                    <div className="job-progress">
                      <div className="label">processing background… {bgJobPct}%</div>
                      <div className="progress-bar"><div style={{ width: `${bgJobPct}%` }} /></div>
                    </div>
                  ) : (
                    <button className="btn-violet" style={{ height: 32 }} onClick={() => processBg(item.id)}>
                      {item.bg.processed ? 'Re-process background preview' : 'Process background preview'}
                    </button>
                  )}
                  {item.bg.stale && (
                    <div className="warn-note">⚠ <span>Clip changed after processing — re-process to refresh the preview.</span></div>
                  )}
                  <div className="foot-note">Final export re-processes at full resolution automatically.</div>
                </>
              )}
            </div>

            {/* Face retouch card */}
            <div className="insp-card">
              <div className="insp-row-between">
                <label className="check">
                  <input type="checkbox" checked={!!rt}
                         onChange={(e) => edit((c) => { c.rt = e.target.checked ? { ...RT_ZERO } : null })} />
                  Face retouch
                </label>
                {rt && <span className="rt-reset" title="Reset (all zeroed)"
                             onClick={() => edit((c) => { c.rt = { ...RT_ZERO, scope: c.rt.scope } })}>↺</span>}
              </div>
              {rt && (
                <>
                  {!track ? (
                    <div className="teal-note detecting"><span className="dot" />detecting face…</div>
                  ) : faceInClip ? (
                    <div className="teal-note"><span className="dot" />1 face detected · tracked across the clip</div>
                  ) : (
                    <div className="warn-note">⚠ <span>No face detected in this clip — retouch won’t apply. Try a clip where the face is visible.</span></div>
                  )}
                  <div className="seg2">
                    <button className={rt.preset === 'natural' ? 'on' : ''}
                            onClick={() => edit((c) => { c.rt = { ...RT_NATURAL, scope: c.rt.scope } })}>Natural</button>
                    <button className={rt.preset === 'studio' ? 'on' : ''}
                            onClick={() => edit((c) => { c.rt = { ...RT_STUDIO, scope: c.rt.scope } })}>Studio</button>
                    <button className={rt.preset === 'custom' ? 'on' : ''} style={{ cursor: 'default' }}>Custom</button>
                  </div>
                  <label className="slider-label rt-intensity">Intensity · {rt.intensity}
                    <input type="range" min="0" max="100" step="1" value={rt.intensity}
                           onPointerDown={beginGesture} onChange={setRt('intensity')} />
                  </label>
                  <div className="rt-finetune-label">Fine-tune</div>
                  {RT_SLIDERS.map(([key, label]) => (
                    <label key={key} className="slider-label">{label} · {rt[key]}
                      <input type="range" min="0" max="100" step="1" value={rt[key]}
                             onPointerDown={beginGesture} onChange={setRt(key)} />
                    </label>
                  ))}
                  {rtStrong && (
                    <div className="warn-note">⚠ <span>Above ~40 skin starts to look airbrushed. Natural keeps pores and texture.</span></div>
                  )}
                  <div className="seg2">
                    <button className={rt.scope !== 'all' ? 'on' : ''}
                            onClick={() => edit((c) => { c.rt.scope = 'clip' })}>This clip</button>
                    <button className={rt.scope === 'all' ? 'on' : ''}
                            onClick={() => edit((c, pp) => {
                              c.rt.scope = 'all'
                              // copy only the LOOK to other clips — not this clip's
                              // processed-preview state / cache pointer (would mislabel
                              // them and alias this clip's cache file)
                              const { _cache, processed, stale, ...tune } = c.rt
                              pp.clips.forEach((o) => { if (o.id !== c.id) o.rt = structuredClone(tune) })
                            })}>All clips</button>
                  </div>
                  {rtJob === item.id ? (
                    <div className="job-progress">
                      <div className="label">processing retouch preview… {rtJobPct}%</div>
                      <div className="progress-bar"><div style={{ width: `${rtJobPct}%` }} /></div>
                    </div>
                  ) : (
                    <button className="btn-violet" style={{ height: 32 }} disabled={!faceInClip}
                            onClick={() => processRt(item.id)}>
                      {rtFresh ? '✓ Preview processed — re-process' : rt.processed ? 'Re-process retouch preview' : 'Process retouch preview'}
                    </button>
                  )}
                  {rt.processed && rt.stale && (
                    <div className="warn-note">⚠ <span>Clip or sliders changed — re-process to refresh the accurate preview.</span></div>
                  )}
                  <div className="foot-note">Sliders give a live face-only approximation; “Process” renders the real pipeline at preview res. Hold “compare” on the preview to check against the original. Export applies at full resolution.</div>
                </>
              )}
            </div>
          </>
        )}

        {/* ── CAPTION ── */}
        {hasSel && sel.type === 'cap' && (
          <>
            <div className="insp-stack">
              <div className="insp-section-label">Caption text</div>
              <label className="field">
                <textarea rows={3}
                          placeholder="Type the caption — words re-sync to the audio automatically"
                          value={capDraft ?? item.words.map((w) => w.w).join(' ')}
                          onFocus={() => {
                            beginGesture()
                            // the ORIGINAL audio span is captured once per editing
                            // session (source seconds for source-anchored — REN-115)
                            const ws = item.words
                            capSpanRef.current = ws.length
                              ? (capSrc ? [ws[0].t0, ws[ws.length - 1].t1]
                                : [ws[0].t, ws[ws.length - 1].t + ws[ws.length - 1].d])
                              : [s.getTime(), s.getTime() + 2]
                          }}
                          onChange={(e) => {
                            const txt = e.target.value
                            setCapDraft(txt)
                            const [t0, t1] = capSpanRef.current || [s.getTime(), s.getTime() + 2]
                            // mark hand-edited so Retranscribe won't overwrite it
                            editLive((c) => { c.words = capSrc ? mkWordsSrcCovered(p, c.src || 'main', txt, t0, t1) : mkWords(txt, t0, t1); c.edited = true })
                          }}
                          onBlur={() => setCapDraft(null)} />
              </label>
              <div className="teal-note"><span className="dot" />
                {item.words.length} words · re-synced to audio {fmtTime(capT0)}–{fmtTime(capT1)}
              </div>
            </div>
            <div className="grid2">
              <label className="field span2">Font
                <select value={item.font} onChange={(e) => edit((c) => { c.font = e.target.value })}>
                  {FONT_NAMES.map((f) => <option key={f} value={f}>{f}</option>)}
                </select>
              </label>
              <label className="field">Color
                <input type="color" value={item.color || '#FFFFFF'} onFocus={beginGesture}
                       onChange={(e) => editLive((c) => { c.color = e.target.value.toUpperCase() })} />
              </label>
              <NumField label="Size (% height)" value={item.size} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editLive((c) => { c.size = v })} />
              <NumField label="Position X (%)" value={item.x} step={1} onFocus={beginGesture}
                        onChange={(v) => editLive((c) => { c.x = v })} />
              <NumField label="Position Y (%)" value={item.y} step={1} onFocus={beginGesture}
                        onChange={(v) => editLive((c) => { c.y = v })} />
              <MaxWField value={item.maxW} onFocus={beginGesture}
                         onLive={(v) => editLive((c) => { c.maxW = v })} />
            </div>
            <label className="slider-label">Unspoken word dim · {item.dim}
              <input type="range" min="0" max="1" step="0.05" value={item.dim}
                     onPointerDown={beginGesture}
                     onChange={(e) => editLive((c) => { c.dim = +e.target.value })} />
            </label>
            <div className="seg2">
              <button className={item.mode === 'karaoke' ? 'on' : ''}
                      onClick={() => edit((c) => { c.mode = 'karaoke' })}>Karaoke</button>
              <button className={item.mode === 'static' ? 'on' : ''}
                      onClick={() => edit((c) => { c.mode = 'static' })}>Static</button>
            </div>
          </>
        )}

        {/* ── HEADLINE ── */}
        {hasSel && sel.type === 'text' && (
          <>
            <label className="field">Text
              <textarea rows={3} value={item.text} onFocus={beginGesture}
                        onChange={(e) => editLive((x) => { x.text = e.target.value })} />
            </label>
            <div className="grid2">
              <label className="field">Color
                <input type="color" value={item.color || '#FFFFFF'} onFocus={beginGesture}
                       onChange={(e) => editLive((x) => { x.color = e.target.value.toUpperCase() })} />
              </label>
              <NumField label="Size (% height)" value={item.size} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editLive((x) => { x.size = v })} />
              <NumField label="Start (s)" value={item.t0} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editLive((x) => { x.t0 = v })} />
              <NumField label="End (s)" value={item.t1} step={0.1} onFocus={beginGesture}
                        onChange={(v) => editLive((x) => { x.t1 = v })} />
              <MaxWField value={item.maxW} onFocus={beginGesture}
                         onLive={(v) => editLive((x) => { x.maxW = v })} />
            </div>
            <label className="check">
              <input type="checkbox" checked={item.shadow !== false}
                     onChange={(e) => edit((x) => { x.shadow = e.target.checked })} />
              Soft shadow
            </label>
          </>
        )}

        {/* ── OVERLAY ── */}
        {hasSel && sel.type === 'ov' && (
          <>
            <div className="mono-chip">{item.name} · {item.kind}</div>
            <div className="grid2">
              <NumField label="X (%)" value={item.x} step={1} onFocus={beginGesture} onChange={(v) => editLive((o) => { o.x = v })} />
              <NumField label="Y (%)" value={item.y} step={1} onFocus={beginGesture} onChange={(v) => editLive((o) => { o.y = v })} />
              <NumField label="Width (%)" value={item.w} step={1} onFocus={beginGesture} onChange={(v) => editLive((o) => { o.w = v })} />
              <label className="field">Height (empty = auto)
                <input type="text" placeholder="auto" value={item.h ?? ''} onFocus={beginGesture}
                       onChange={(e) => {
                         const v = parseFloat(e.target.value)
                         editLive((o) => { o.h = Number.isFinite(v) ? v : null })
                       }} />
              </label>
              <NumField label="Start (s)" value={item.t0} step={0.1} onFocus={beginGesture} onChange={(v) => editLive((o) => { o.t0 = v })} />
              <NumField label="End (s)" value={item.t1} step={0.1} onFocus={beginGesture} onChange={(v) => editLive((o) => { o.t1 = v })} />
            </div>
            {item.kind === 'image' && (
              <button className="btn-violet" style={{ height: 32 }} disabled={cutBusy} onClick={toggleCut}>
                {cutBusy ? 'Removing background…' : item.cut ? '✓ Background removed — undo' : 'Remove image background'}
              </button>
            )}
            {item.kind === 'video' && (
              <>
                <label className="slider-label">Volume · {item.vol}
                  <input type="range" min="0" max="1" step="0.05" value={item.vol}
                         onPointerDown={beginGesture}
                         onChange={(e) => editLive((o) => { o.vol = +e.target.value })} />
                </label>
                <div className="seg2">
                  <button className={item.crop !== 'top' ? 'on' : ''}
                          onClick={() => edit((o) => { o.crop = 'center' })}>Crop center</button>
                  <button className={item.crop === 'top' ? 'on' : ''}
                          onClick={() => edit((o) => { o.crop = 'top' })}>Crop top</button>
                </div>
              </>
            )}
          </>
        )}

        {/* ── AUDIO ── */}
        {hasSel && sel.type === 'aud' && (
          <>
            <div className="mono-chip">♪ {item.name}</div>
            <label className="slider-label">Volume · {item.vol}
              <input type="range" min="0" max="2" step="0.05" value={item.vol}
                     onPointerDown={beginGesture}
                     onChange={(e) => editLive((a) => { a.vol = +e.target.value })} />
            </label>
            <div className="grid2">
              <NumField label="Start (s)" value={item.t0} step={0.1} onFocus={beginGesture} onChange={(v) => editLive((a) => { a.t0 = v })} />
              <NumField label="End (s)" value={item.t1} step={0.1} onFocus={beginGesture} onChange={(v) => editLive((a) => { a.t1 = v })} />
              <NumField label="Fade in (s)" value={item.fadeIn} step={0.1} onFocus={beginGesture} onChange={(v) => editLive((a) => { a.fadeIn = v })} />
              <NumField label="Fade out (s)" value={item.fadeOut} step={0.1} onFocus={beginGesture} onChange={(v) => editLive((a) => { a.fadeOut = v })} />
            </div>
          </>
        )}
      </div>
    </div>
  )
}
