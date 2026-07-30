import { useEffect, useMemo, useRef, useState } from 'react'
import { fileUrl } from './api'
import { FONTS_CSS, capSpan, clipOutStart, itemAlpha, fmtTime, materializeCap, mkWords, mkWordsSrcCovered, outDuration, outToSource, trackFlag, typeLocked, zoomAt } from './time'
import { timeStore, useTimeValue } from './timeStore'

const SYNC_EPS = 0.12
// While PAUSED the tolerance has to be under one frame, or the precision
// controls lie: a frame step moves 1/30s = 0.033s, so a 0.12s dead zone meant
// three presses out of four changed the clock and left the picture alone, then
// the fourth jumped four frames. Same for slow playhead dragging. The loose
// value stays for playback, where it prevents seeking on ordinary drift.
const PAUSE_EPS = 0.01
// How long before a cut the next clip starts rolling out of sight. It has to
// comfortably clear the measured 188ms that play() takes to paint its first
// frame on a paused element.
const ARM_LEAD = 0.7
// The cut may wait at most this long for the incoming buffer to have a frame.
// Bounded because while it waits the outgoing element rolls past its out-point,
// showing footage the export does not contain.
const HOLD_MAX = 0.25
const EDGE_EPS = 1 / 60
const CLICK_SLOP = 4 // px — pointerdown→up under this is a click, not a drag

// Headline text wrapped so lines break ONLY at spaces, exactly like the
// export's greedy wrap — plain pre-wrap would also break after intra-word
// hyphens (segunda-feira) and diverge from the renderer.
const wrapSafe = (text) => String(text ?? '').split('\n').flatMap((line, li) => {
  const parts = li > 0 ? ['\n'] : []
  line.split(' ').forEach((wd, wi) => {
    if (wi > 0) parts.push(' ')
    parts.push(<span key={`${li}-${wi}`} style={{ whiteSpace: 'nowrap' }}>{wd}</span>)
  })
  return parts
})

export default function Player({ s, onImportVideo }) {
  const { project: p, playing, setPlaying, sel, setSel,
          mutate, beginGesture, fullscreen, setFullscreen, compare, setCompare,
          isMobile, processBg, inlineEdit, setInlineEdit, faceTracks, rtSig,
          showToast } = s
  const time = useTimeValue()
  const frameRef = useRef(null)
  // Two <video> elements per source, alternating (REN-137). With one element a
  // cut meant `el.currentTime = next.in`, and a seek costs 100-400ms of decoder
  // flush — so the preview froze at every take while the exported file did not.
  // The idle buffer is seeked to the NEXT clip during the current one, and the
  // cut is then just a swap of which element is visible.
  const srcRefs = useRef({})
  const srcRefsB = useRef({})
  const bufRef = useRef(0)
  const srcEl = (key, b = bufRef.current) => (b ? srcRefsB : srcRefs).current[key]
  const cacheRefs = useRef({})
  const ovRefs = useRef({})
  const audRefs = useRef({})
  const rtRefs = useRef({})          // clip id -> processed-retouch preview video
  const rtLiveRef = useRef(null)     // live face-masked approximation layer
  const projRef = useRef(p)
  const engineRef = useRef({ ci: -1, el: null })
  const armedRef = useRef({ el: null, ci: -1 })   // buffer already rolling for the next cut
  const parkedRef = useRef({ el: null, ci: -1, to: 0 })  // idle buffer pre-seeked a whole clip early
  const gateRef = useRef(0)                              // ms stamp of an in-progress swap hold
  const compareRef = useRef(compare)
  compareRef.current = compare
  const pointersRef = useRef({})     // element pinch: pointerId -> {x, y}
  const pinchedRef = useRef(false)   // gesture escalated to a pinch — no click-to-edit
  const editSpanRef = useRef(null)   // caption audio span captured at edit start
  const editInitRef = useRef(null)   // which inlineEdit session seeded the DOM
  const editElRef = useRef(null)     // live DOM node of the active inline editor
  const editItemRef = useRef(null)   // {type, item} of the active session
  const [frameH, setFrameH] = useState(480)
  // track mute (REN-112) via a ref so the running engine sees live toggles
  const muteRef = useRef({})
  muteRef.current = {
    video: trackFlag(p, 'video', 'muted'),
    audio: trackFlag(p, 'audio', 'muted'),
    overlays: trackFlag(p, 'overlays', 'muted'),
  }
  const videoHidden = trackFlag(p, 'video', 'hidden')
  const capsHidden = trackFlag(p, 'captions', 'hidden')
  const textsHidden = trackFlag(p, 'texts', 'hidden')
  const ovHidden = trackFlag(p, 'overlays', 'hidden')
  projRef.current = p
  // Web Audio gain so per-clip / per-track volume can exceed 100% in the preview
  // (an HTML <video>.volume caps at 1.0; a GainNode boosts up to 2×, matching
  // what ffmpeg does on export). Falls back to the capped el.volume if Web Audio
  // is unavailable, so audio is never lost.
  const audioCtxRef = useRef(null)
  const gainMap = useRef(new Map()) // media element -> GainNode (null = unsupported)
  // Boot + unlock the audio context inside a real user gesture so Safari/iOS
  // lifts 'suspended' (Chrome is lenient). Tear it down on unmount (Back to
  // projects) — Chrome caps concurrent contexts and would eventually throw,
  // silently killing the >100% boost for the rest of the session.
  useEffect(() => {
    const unlock = () => {
      let ctx = audioCtxRef.current
      if (!ctx) {
        try { const AC = window.AudioContext || window.webkitAudioContext; ctx = new AC(); audioCtxRef.current = ctx } catch { return }
        // the context is created lazily on first gesture — carry the chosen
        // output over, or the picker silently reverts to default on every load
        if (sinkRef.current) ctx.setSinkId?.(sinkRef.current).catch(() => {})
      }
      if (ctx.state === 'suspended') ctx.resume().catch(() => {})
    }
    window.addEventListener('pointerdown', unlock)
    window.addEventListener('keydown', unlock)
    return () => {
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
      const ctx = audioCtxRef.current
      if (ctx && ctx.state !== 'closed') ctx.close().catch(() => {})
      audioCtxRef.current = null
      gainMap.current.clear()
    }
  }, [])

  const dur = useMemo(() => outDuration(p), [p])
  const [ci] = outToSource(p, time)
  const clip = ci != null ? p.clips[ci] : null
  const clipStart = ci != null ? clipOutStart(p, ci) : 0

  useEffect(() => {
    const el = frameRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setFrameH(el.clientHeight || 480))
    ro.observe(el)
    return () => ro.disconnect()
  }, [p.clips.length > 0, fullscreen])

  const cacheFresh = (c) => c.bg?.processed && !c.bg.stale && c.bg._cache &&
    c.bg._cache.in === c.in && c.bg._cache.out === c.out && c.bg._cache.path

  // processed face-retouch preview for this clip is usable (REN-84)
  const rtCacheFresh = (c) => {
    const rc = c.rt?._cache
    return !!(c.rt?.processed && !c.rt.stale && rc && rc.path &&
      rc.in === c.in && rc.out === c.out && rc.sig === rtSig(c.rt))
  }

  // Which video element supplies the clip's base image, and how its currentTime
  // maps to timeline time. Processed caches (bg/retouch) are standalone clips
  // ([in,out] rendered to 0-based files), so their time is clip-relative;
  // the raw source is absolute (c.in + rel). "compare" hold falls back to the
  // untouched source so the retouch/bg preview can be checked against original.
  // compareRef (not the compare prop) so the running engine sees toggles without
  // being torn down and rebuilt — tick swaps the base element in place instead
  function pick(c) {
    if (!compareRef.current && rtCacheFresh(c) && rtRefs.current[c.id])
      return { el: rtRefs.current[c.id], cache: true, kind: 'rt' }
    if (!compareRef.current && cacheFresh(c) && cacheRefs.current[c.id])
      return { el: cacheRefs.current[c.id], cache: true, kind: 'bg' }
    return { el: srcEl(c.src || 'main'), cache: false, kind: 'src' }
  }
  const allBaseEls = () => [
    ...Object.values(srcRefs.current), ...Object.values(srcRefsB.current),
    ...Object.values(cacheRefs.current), ...Object.values(rtRefs.current),
  ]
  function showOnly(activeEl) {
    for (const el of allBaseEls()) {
      if (!el) continue
      const on = el === activeEl
      // opacity, NOT display. A display:none <video> is dropped from the render
      // tree and Chrome discards its decoded frames, so the buffer we spent the
      // whole previous clip preparing still had to warm up at the cut — the
      // freeze survived the double-buffer for exactly this reason. Kept
      // composited, the swap is instant.
      el.style.display = 'block'
      el.style.opacity = on ? '1' : '0'
      el.style.zIndex = on ? '1' : '0'
      if (on) el.muted = false
      // never pause the buffer that is being armed for the next cut — pausing
      // it is precisely what costs 188ms when it is brought on screen
      if (!on && !el.paused && el !== armedRef.current.el) el.pause()
    }
  }

  // ---- audio output routing (REN-140) -------------------------------------
  // The DEVICE choice lives in the store (the header owns the control); this is
  // only the part that has to sit next to the media elements: putting the chosen
  // sink on each of them, including the ones that mount later.
  const sinkSupported = typeof HTMLMediaElement !== 'undefined'
    && 'setSinkId' in HTMLMediaElement.prototype
  const sinkId = s.sinkId || ''
  const sinkRef = useRef(sinkId)
  sinkRef.current = sinkId

  function routeSink(el) {
    if (!sinkSupported || !el || el._vsSink === sinkRef.current) return
    const want = sinkRef.current
    el._vsSink = want
    el.setSinkId?.(want).catch(() => {
      if (want && sinkRef.current === want) {
        s.setSinkId('')
        showToast?.('That audio output is gone — switched back to the system one.')
      }
    })
  }

  useEffect(() => {
    if (!sinkSupported) return
    // the context only exists when a clip needed >100% volume; keep it in step
    audioCtxRef.current?.setSinkId?.(sinkId).catch(() => {})
    for (const el of [...allBaseEls(), rtLiveRef.current,
                      ...Object.values(ovRefs.current), ...Object.values(audRefs.current)]) {
      if (el) { el._vsSink = undefined; routeSink(el) }
    }
  }, [sinkId])

  // interpolated normalized face box at the playhead (for the live mask)
  function faceBoxAt(c, srcT) {
    const tr = faceTracks?.[c.src || 'main']
    const sm = tr?.samples
    if (!sm || !sm.length) return null
    let lo = sm[0], hi = sm[sm.length - 1]
    for (let i = 0; i < sm.length; i++) {
      if (sm[i].t <= srcT) lo = sm[i]
      if (sm[i].t >= srcT) { hi = sm[i]; break }
    }
    // only trust the box when a sample is reasonably near (face present here)
    if (Math.min(Math.abs(lo.t - srcT), Math.abs(hi.t - srcT)) > 0.6) return null
    const p = hi.t === lo.t ? 0 : (srcT - lo.t) / (hi.t - lo.t)
    const lerp = (a, b) => a + (b - a) * p
    return { x: lerp(lo.x, hi.x), y: lerp(lo.y, hi.y), w: lerp(lo.w, hi.w), h: lerp(lo.h, hi.h) }
  }

  function clipFade(c, rel) {
    const d = c.out - c.in
    let f = 1
    if (c.fadeIn && rel < c.fadeIn) f = Math.min(f, rel / c.fadeIn)
    if (c.fadeOut && rel > d - c.fadeOut) f = Math.min(f, Math.max(0, (d - rel) / c.fadeOut))
    return f
  }
  // (The audio declick lives in the EXPORT renderer — a per-frame gain step in
  // the preview can't ramp smoothly and would just dip the audio at each cut, so
  // the preview uses clipFade and the exported file is the click-free source.)

  // lazily route an element's audio through a GainNode (once per element); the
  // Map guards against a second createMediaElementSource on the same element.
  function gainFor(el) {
    if (!el) return null
    const m = gainMap.current
    if (m.has(el)) return m.get(el) // already routed (or known-unsupported)
    const ctx = audioCtxRef.current
    // Only interpose Web Audio once the context can actually output sound.
    // createMediaElementSource PERMANENTLY reroutes the element's audio into the
    // graph, so doing it while the context is 'suspended' (Safari/iOS before a
    // real gesture unlocks it) would SILENCE the clip. Until it's 'running',
    // return null so setVol keeps the plain el.volume path — audio always plays,
    // just capped at 1.0 until the boost path is safely available. Not cached, so
    // it retries next tick once the context unlocks.
    if (!ctx || ctx.state !== 'running') return null
    let g = null
    try {
      const src = ctx.createMediaElementSource(el)
      g = ctx.createGain()
      src.connect(g); g.connect(ctx.destination)
    } catch { g = null } // already-connected or unsupported → fall back to el.volume
    m.set(el, g)
    return g
  }
  // set final linear amplitude (0..2). Uses the GainNode when available so it can
  // exceed 1.0; otherwise clamps to the <video>.volume ceiling (never silent).
  function setVol(el, amp) {
    if (!el) return
    // Every element that produces sound comes through here, including ones that
    // mount long after the sink effect ran (overlay videos are time-gated and
    // remount as the playhead crosses them; a bg/retouch cache appears when its
    // job finishes). Routing here instead of in an effect is what makes the
    // picker actually cover them.
    routeSink(el)
    // Only put an element on the Web Audio graph when it genuinely needs to go
    // ABOVE 100% — which is rare. Routing everything through
    // createMediaElementSource cost two real bugs: the element's own sinkId
    // stops deciding where the sound goes (so the output picker did nothing),
    // and Chrome's element→graph path drifts further out of sync the longer a
    // session runs (audio arriving well after the picture, only cured by
    // restarting the app). The routing is irreversible per element, so the
    // decision has to be made BEFORE the node exists — hence the check here and
    // not inside gainFor.
    const boost = amp > 1.001
    const g = (boost || gainMap.current.has(el)) ? gainFor(el) : null
    if (g) { el.volume = 1; g.gain.value = Math.max(0, amp) }
    else el.volume = Math.max(0, Math.min(1, amp))
  }

  function syncEl(el, want, shouldPlay, volume = null) {
    if (!el) return
    if (Math.abs(el.currentTime - want) > (shouldPlay ? SYNC_EPS : PAUSE_EPS)) el.currentTime = want
    if (volume != null) setVol(el, volume)
    if (shouldPlay && el.paused) el.play().catch(() => {})
    if (!shouldPlay && !el.paused) el.pause()
  }

  // Non-main media (music tracks, video overlays) follow the timeline clock
  // with loose drift correction — the main video is never seeked mid-play.
  function syncSecondary(t, isPlaying) {
    const proj = projRef.current
    for (const o of proj.overlays || []) {
      if (o.kind !== 'video') continue
      const el = ovRefs.current[o.id]
      if (!el) continue
      const t1 = o.t1 ?? dur
      const active = t >= o.t0 && t < t1
      if (active) {
        const a = itemAlpha(t, o.t0, t1, o.fadeIn, o.fadeOut)
        syncEl(el, t - o.t0, isPlaying, (o.vol || 0) * a * (muteRef.current.overlays ? 0 : 1))
      } else if (!el.paused) el.pause()
    }
    for (const a of proj.audios || []) {
      const el = audRefs.current[a.id]
      if (!el) continue
      const t1 = a.t1 ?? dur
      const active = t >= a.t0 && t < t1
      if (active) {
        const fa = itemAlpha(t, a.t0, t1, a.fadeIn, a.fadeOut)
        syncEl(el, t - a.t0, isPlaying, (a.vol ?? 1) * fa * (muteRef.current.audio ? 0 : 1))
      } else if (!el.paused) el.pause()
    }
  }

  // live retouch approximation follows the source at source-time (only mounted
  // when the base is the raw source, so it can never fight a processed swap)
  function syncLive(srcT, isPlaying) {
    const el = rtLiveRef.current
    if (el) syncEl(el, srcT, isPlaying)
  }

  // Full pass used while PAUSED (scrub / frame-step / project reload)
  function syncAll(t) {
    const proj = projRef.current
    const [i, srcT] = outToSource(proj, t)
    if (i == null) return
    const c = proj.clips[i]
    const { el, cache } = pick(c)
    showOnly(el)
    if (el) syncEl(el, cache ? t - clipOutStart(proj, i) : srcT, false)
    syncLive(srcT, false)
    syncSecondary(t, false)
  }

  // ---- playback engine: the active video element is the master clock ----
  useEffect(() => {
    if (!playing) return
    let raf, iv
    let stopped = false

    const enter = (i, rel) => {
      const proj = projRef.current
      const c = proj.clips[i]
      if (!c) { stop(); return }
      engineRef.current.ci = i
      const { el, cache } = pick(c)
      engineRef.current.el = el
      showOnly(el)
      if (!el) { stop(); return }
      const want = cache ? rel : c.in + rel
      // An element ARMED for this cut is already rolling within a frame or two
      // of `want`. Seeking it would throw away exactly what the arming bought,
      // so accept the sub-frame offset and let it run.
      // `!paused` only proves play() was CALLED. An element whose arm seek is
      // still outstanding reports paused=false with readyState 1, cannot paint,
      // and sits up to ARM_LEAD before its own in-point — so trusting it both
      // froze the picture AND started the take at the wrong place. Demand a
      // decodable frame. (Measured: `armed` was true at 100% of ~500 cuts,
      // including ones with 830ms and 2322ms holes. readyState is the real test.)
      const armed = armedRef.current.el === el && !el.paused
        && !el.seeking && el.readyState >= 3
      if (!armed && Math.abs(el.currentTime - want) > EDGE_EPS * 2) el.currentTime = want
      setVol(el, clipFade(c, rel) * (c.vol ?? 1) * (muteRef.current.video ? 0 : 1))
      if (el.paused) el.play().catch(() => {})
      armedRef.current = { el: null, ci: -1 }

      // Take the SEEK out of the hot window. Park the idle buffer on the next
      // clip's in-point right now, while this clip has its whole duration to
      // finish fetching and decoding; arm() then only has to press play.
      const nx = proj.clips[i + 1]
      if (nx) {
        const nxCache = (!compareRef.current && rtCacheFresh(nx) && rtRefs.current[nx.id])
          || (!compareRef.current && cacheFresh(nx) && cacheRefs.current[nx.id]) || null
        const idle = nxCache || srcEl(nx.src || 'main', bufRef.current ? 0 : 1)
        // never touch what is on screen, never scrub one already armed
        if (idle && idle !== el && idle !== armedRef.current.el) {
          const lead = Math.min(ARM_LEAD, Math.max(0, c.out - c.in))
          const to = Math.max(0, (nxCache ? 0 : nx.in) - lead)
          idle.muted = true
          if (!idle.paused) idle.pause()
          if (Math.abs(idle.currentTime - to) > EDGE_EPS) idle.currentTime = to
          parkedRef.current = { el: idle, ci: i + 1, to }
        }
      }
      const live = rtLiveRef.current
      if (live) {
        if (Math.abs(live.currentTime - (c.in + rel)) > EDGE_EPS * 2) live.currentTime = c.in + rel
        live.play().catch(() => {})
      }
    }

    // THE fix for the freeze between takes. Measured on this machine: calling
    // play() on a PAUSED <video> takes 188ms to paint its first frame; on one
    // that is already rolling it is 40ms — a single frame. Parking the idle
    // buffer at the right time was never enough, because it stayed PAUSED and
    // the cut still paid that 188ms every time. So the next clip now starts
    // ROLLING (muted, invisible) ARM_LEAD seconds before the cut, positioned so
    // it reaches its own first frame exactly when we swap. The swap is then a
    // change of opacity and nothing else.
    const arm = (i, tLeft) => {
      const proj = projRef.current
      const nx = proj.clips[i + 1]
      if (!nx || armedRef.current.ci === i + 1) return
      // Which element will SHOW the next clip: its own processed cache, or the
      // OTHER source buffer — the buffers swap at the cut, so pick() (which
      // resolves against the CURRENT buffer) would hand back the element that
      // is on screen right now and arming would scrub the live picture.
      const cacheEl = (!compareRef.current && rtCacheFresh(nx) && rtRefs.current[nx.id])
        || (!compareRef.current && cacheFresh(nx) && cacheRefs.current[nx.id]) || null
      const el = cacheEl || srcEl(nx.src || 'main', bufRef.current ? 0 : 1)
      if (!el || el === engineRef.current.el) return
      armedRef.current = { el, ci: i + 1 }
      el.muted = true
      const base = cacheEl ? 0 : nx.in
      const to = Math.max(0, base - Math.max(0, tLeft))
      const pk = parkedRef.current
      // Reuse the park only if it is this element, this clip, settled, and still
      // pointing where we want — the clip may have been edited since.
      const preParked = pk.el === el && pk.ci === i + 1 && !el.seeking
        && Math.abs(el.currentTime - pk.to) < 0.10 && Math.abs(pk.to - to) < 0.15
      if (!preParked) el.currentTime = to
      el.play().catch(() => {})
    }

    // Park the idle buffer on the next clip's first frame while this one plays,
    // so the swap at the cut costs nothing. Clips with their own processed
    // element (bg / retouch cache) already have a dedicated <video> and never
    // stalled, so they need no prefetch.
    const stop = () => {
      if (stopped) return
      stopped = true
      setPlaying(false)
      timeStore.set(0)
      syncAll(0)
    }

    const tick = () => {
      if (stopped) return
      const proj = projRef.current
      const i = engineRef.current.ci
      const c = proj.clips[i]
      if (!c) {
        const [ni] = outToSource(proj, timeStore.get())
        if (ni == null) { stop(); return }
        enter(ni, Math.max(0, timeStore.get() - clipOutStart(proj, ni)))
        return
      }
      const { el, cache } = pick(c)
      if (!el) return
      // base element changed mid-clip (a processed cache became fresh, or the
      // compare hold toggled) — re-activate it at the current timeline position
      // instead of reading its paused, t=0 clock (which would freeze the playhead)
      if (el !== engineRef.current.el) {
        enter(i, Math.max(0, timeStore.get() - clipOutStart(proj, i)))
        return
      }
      const rel = cache ? el.currentTime : el.currentTime - c.in
      const cDur = c.out - c.in
      // start the next clip rolling while this one is still on screen
      const left = cDur - rel
      if (left <= ARM_LEAD) arm(i, left)
      if (rel >= cDur - EDGE_EPS || el.ended) {
        const j = i + 1
        if (j < proj.clips.length) {
          const nc = proj.clips[j]
          const nxCache = (!compareRef.current && rtCacheFresh(nc) && rtRefs.current[nc.id])
            || (!compareRef.current && cacheFresh(nc) && cacheRefs.current[nc.id]) || null
          const inc = nxCache || srcEl(nc.src || 'main', bufRef.current ? 0 : 1)
          const tb = clipOutStart(proj, j)
          // Swapping onto a buffer that cannot paint IS the freeze. Rather than
          // put a dead picture on screen, let the outgoing element run a few
          // frames past its out-point — bounded, because that footage was cut on
          // purpose. Measured under a slow transport: 6/6 cuts froze 227-1321ms
          // without this; 0/6 with it, first paint 15-33ms.
          if (inc && inc !== el && !el.ended && (inc.seeking || inc.readyState < 3)) {
            if (!gateRef.current) gateRef.current = performance.now()
            if (performance.now() - gateRef.current < HOLD_MAX * 1000) {
              timeStore.set(tb)               // clock, captions and overlays keep moving
              setVol(el, clipFade(c, cDur) * (c.vol ?? 1) * (muteRef.current.video ? 0 : 1))
              syncLive(c.in + rel, true)
              syncSecondary(tb, true)
              return
            }
          }
          gateRef.current = 0
          timeStore.set(tb)
          if (!nxCache) bufRef.current = bufRef.current ? 0 : 1
          enter(j, 0)
        } else {
          stop()
        }
        return
      }
      const t = clipOutStart(proj, i) + Math.max(0, rel)
      timeStore.set(t)
      setVol(el, clipFade(c, rel) * (c.vol ?? 1) * (muteRef.current.video ? 0 : 1))
      syncLive(c.in + rel, true)
      syncSecondary(t, true)
    }

    const t0 = timeStore.get()
    const proj = projRef.current
    if (!proj.clips.length) { setPlaying(false); return }
    const [si] = outToSource(proj, t0)
    enter(si ?? 0, Math.max(0, t0 - clipOutStart(proj, si ?? 0)))

    const loop = () => { tick(); raf = requestAnimationFrame(loop) }
    raf = requestAnimationFrame(loop)
    iv = setInterval(tick, 250) // hidden-tab fallback (rAF is throttled there)

    return () => {
      stopped = true
      cancelAnimationFrame(raf)
      clearInterval(iv)
      armedRef.current = { el: null, ci: -1 }
      parkedRef.current = { el: null, ci: -1, to: 0 }
      gateRef.current = 0
      for (const el of [...allBaseEls(), rtLiveRef.current,
                        ...Object.values(ovRefs.current), ...Object.values(audRefs.current)]) {
        if (el && !el.paused) el.pause()
      }
    }
    // compare + processed-cache swaps are handled inside tick (el-change re-enter),
    // so the engine no longer restarts on compare — music/overlays never blip
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing])

  // Discrete time changes while paused (scrub, frame-step, external reload)
  useEffect(() => {
    if (!playing) syncAll(time)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [time, playing, p, compare])

  // ---- inline text editing (REN-79) ----
  const isEditing = (type, id) => inlineEdit && inlineEdit.type === type && inlineEdit.id === id

  function enterEdit(type, item) {
    setPlaying(false)
    beginGesture() // one undo entry per editing session
    if (type === 'cap') {
      const ws = item.words || []
      if (item.capAnchor === 'source') {
        // source span; empty group → source-time under the playhead
        const [ci, st] = outToSource(projRef.current, timeStore.get())
        editSpanRef.current = ws.length ? [ws[0].t0, ws[ws.length - 1].t1]
          : [ci != null ? st : 0, (ci != null ? st : 0) + 2]
      } else {
        editSpanRef.current = ws.length
          ? [ws[0].t, ws[ws.length - 1].t + ws[ws.length - 1].d]
          : [timeStore.get(), timeStore.get() + 2]
      }
    }
    editInitRef.current = null
    setInlineEdit({ type, id: item.id })
  }

  const seedEditable = (type, item, text) => (el) => {
    if (el) editElRef.current = el
    if (!el || editInitRef.current === item.id) return
    editInitRef.current = item.id
    editItemRef.current = { type, item }
    el.textContent = text
    el.focus()
    const range = document.createRange()
    range.selectNodeContents(el)
    range.collapse(false)
    const s2 = window.getSelection()
    s2.removeAllRanges()
    s2.addRange(range)
  }

  function commitEdit() {
    const ctx = editItemRef.current
    const el = editElRef.current
    if (!ctx || !el) return
    editItemRef.current = null
    editInitRef.current = null
    editElRef.current = null
    const txt = (el.innerText || '').replace(/\n+$/, '')
    const proj = projRef.current
    if (!txt.trim()) {
      mutate((pp) => {
        const key = ctx.type === 'cap' ? 'captions' : 'texts'
        pp[key] = pp[key].filter((x) => x.id !== ctx.item.id)
      }, false)
      setSel(null)
    } else if (ctx.type === 'cap') {
      // write the on-screen text back — if the project was reloaded under our
      // feet (chat reply, external edit) the DOM is the user's source of truth
      const [t0, t1] = editSpanRef.current || [timeStore.get(), timeStore.get() + 2]
      const words = ctx.item.capAnchor === 'source'
        ? mkWordsSrcCovered(proj, ctx.item.src || 'main', txt, t0, t1) : mkWords(txt, t0, t1)
      const cur = proj?.captions.find((x) => x.id === ctx.item.id)
      if (cur && cur.words.map((w) => w.w).join(' ') !== words.map((w) => w.w).join(' ')) {
        mutate((pp) => {
          const c2 = pp.captions.find((x) => x.id === ctx.item.id)
          if (c2) c2.words = words
        }, false)
      }
    } else {
      const cur = proj?.texts.find((x) => x.id === ctx.item.id)
      if (cur && cur.text !== txt) {
        mutate((pp) => {
          const x2 = pp.texts.find((x) => x.id === ctx.item.id)
          if (x2) x2.text = txt
        }, false)
      }
    }
    setInlineEdit(null)
  }

  // clicking anywhere outside the editor commits — divs don't steal focus, so
  // blur alone would leave the session hanging
  useEffect(() => {
    if (!inlineEdit) return
    const onDown = (e) => {
      if (editElRef.current && !editElRef.current.contains(e.target)) commitEdit()
    }
    window.addEventListener('pointerdown', onDown, true)
    return () => window.removeEventListener('pointerdown', onDown, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inlineEdit])

  const editKeyDown = (type, item) => (e) => {
    e.stopPropagation()
    if (e.key === 'Escape') { e.preventDefault(); e.target.blur(); commitEdit() }
    if (e.key === 'Enter') {
      e.preventDefault()
      if (type === 'cap') { e.target.blur(); commitEdit() } // captions have no manual breaks
      else document.execCommand('insertText', false, '\n')
    }
  }

  const editInput = (type, item) => (e) => {
    const txt = e.target.innerText
    if (type === 'cap') {
      const [t0, t1] = editSpanRef.current || [timeStore.get(), timeStore.get() + 2]
      mutate((pp) => {
        const c2 = pp.captions.find((x) => x.id === item.id)
        if (c2) c2.words = item.capAnchor === 'source'
          ? mkWordsSrcCovered(pp, item.src || 'main', txt, t0, t1) : mkWords(txt, t0, t1)
      }, false)
    } else {
      mutate((pp) => {
        const x2 = pp.texts.find((x) => x.id === item.id)
        if (x2) x2.text = txt
      }, false)
    }
  }

  // ---- move drag + click-to-edit + two-finger pinch (REN-79/80) ----
  // Every gesture owns exactly ONE pointer (its move/up handlers filter on
  // ev.pointerId); the pinch owns the whole pointer set and only tears down
  // when the LAST finger lifts — otherwise a mid-pinch pointerup orphans an
  // entry in pointersRef and every later drag reads as a phantom pinch.
  function elementPointerDown(e, type, item, listName) {
    if (isEditing(type, item.id)) return // caret interactions only
    if (typeLocked(p, type)) return // locked track: no select/move/edit in the preview
    e.stopPropagation()
    e.preventDefault()
    const wasSelected = sel && sel.type === type && sel.id === item.id
    setSel({ type, id: item.id })
    const ptrs = pointersRef.current
    ptrs[e.pointerId] = { x: e.clientX, y: e.clientY }
    const ids = Object.keys(ptrs).filter((k) => k !== '__pinch')

    if (ids.length >= 2) {
      // second finger: switch to pinch-resize (shares the gesture's undo entry)
      if (!ptrs.__pinch) startPinch(type, item, listName)
      return
    }
    pinchedRef.current = false

    beginGesture()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
    const pid = e.pointerId
    const sx = e.clientX, sy = e.clientY
    const ox = item.x ?? 50, oy = item.y ?? 50
    let moved = false
    const move = (ev) => {
      if (ev.pointerId !== pid || pointersRef.current.__pinch) return
      if (pointersRef.current[pid]) {
        pointersRef.current[pid] = { x: ev.clientX, y: ev.clientY }
      }
      const rect = frameRef.current?.getBoundingClientRect()
      if (!rect || rect.width < 2 || rect.height < 2) return
      if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > CLICK_SLOP) moved = true
      if (!moved) return
      const nx = Math.max(2, Math.min(98, ox + ((ev.clientX - sx) / rect.width) * 100))
      const ny = Math.max(2, Math.min(98, oy + ((ev.clientY - sy) / rect.height) * 100))
      mutate((pp) => {
        const it = pp[listName].find((q) => q.id === item.id)
        if (it) { it.x = +nx.toFixed(1); it.y = +ny.toFixed(1) }
      }, false)
    }
    const up = (ev) => {
      if (ev.pointerId !== pid) return
      const wasPinch = pinchedRef.current || !!pointersRef.current.__pinch
      delete pointersRef.current[pid]
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
      // a plain click on an already-selected text enters inline editing
      if (!moved && !wasPinch && wasSelected && (type === 'cap' || type === 'text')) enterEdit(type, item)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  function startPinch(type, item, listName) {
    const ptrs = pointersRef.current
    ptrs.__pinch = true
    pinchedRef.current = true
    const ids = Object.keys(ptrs).filter((k) => k !== '__pinch')
    const d0 = Math.hypot(ptrs[ids[0]].x - ptrs[ids[1]].x, ptrs[ids[0]].y - ptrs[ids[1]].y) || 1
    const size0 = item.size, w0 = item.w, h0 = item.h
    const move = (ev) => {
      if (pointersRef.current[ev.pointerId]) {
        pointersRef.current[ev.pointerId] = { x: ev.clientX, y: ev.clientY }
      }
      const cur = pointersRef.current
      const live = Object.keys(cur).filter((k) => k !== '__pinch')
      if (live.length < 2) return
      const d = Math.hypot(cur[live[0]].x - cur[live[1]].x, cur[live[0]].y - cur[live[1]].y) || 1
      const ratio = d / d0
      mutate((pp) => {
        const it = pp[listName].find((q) => q.id === item.id)
        if (!it) return
        if (type === 'ov') {
          it.w = +Math.max(4, Math.min(100, w0 * ratio)).toFixed(1)
          if (h0) it.h = +Math.max(4, Math.min(100, h0 * ratio)).toFixed(1)
        } else {
          it.size = +Math.max(1, Math.min(20, size0 * ratio)).toFixed(2)
        }
      }, false)
    }
    const up = (ev) => {
      delete pointersRef.current[ev.pointerId]
      const left = Object.keys(pointersRef.current).filter((k) => k !== '__pinch')
      if (left.length === 0) {
        pointersRef.current = {} // pointer-complete teardown — no ghosts survive
        window.removeEventListener('pointermove', move)
        window.removeEventListener('pointerup', up)
        window.removeEventListener('pointercancel', up)
      }
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  // ---- resize handles (REN-80) ----
  // Handles live on the (unzoomed) UI layer; math uses the element's center in
  // frame coordinates, so resizing works identically under clip zoom.
  function handleDown(e, pos, type, item, listName) {
    if (typeLocked(p, type)) return // locked track: no resize handles
    e.stopPropagation()
    e.preventDefault()
    beginGesture()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
    const rect = frameRef.current.getBoundingClientRect()
    const cx = rect.left + (item.x ?? 50) / 100 * rect.width
    const cy = rect.top + (item.y ?? 50) / 100 * rect.height
    const d0 = Math.hypot(e.clientX - cx, e.clientY - cy) || 1
    const size0 = item.size
    const w0 = item.w
    const elRect = e.currentTarget.parentElement.getBoundingClientRect()
    const h0auto = +(elRect.height / rect.height * 100).toFixed(1)
    const h0 = item.h ?? h0auto
    const isCorner = pos.length === 2 // 'tl' | 'tr' | 'bl' | 'br'
    const pid = e.pointerId
    const move = (ev) => {
      if (ev.pointerId !== pid) return
      mutate((pp) => {
        const it = pp[listName].find((q) => q.id === item.id)
        if (!it) return
        if (type === 'ov') {
          if (isCorner || pos === 'l' || pos === 'r') {
            const newW = Math.max(4, Math.min(100, Math.abs(ev.clientX - cx) * 2 / rect.width * 100))
            it.w = +newW.toFixed(1)
            if (isCorner && item.h != null) it.h = +Math.max(4, Math.min(100, h0 * (newW / w0))).toFixed(1)
          }
          if (pos === 't' || pos === 'b') {
            it.h = +Math.max(4, Math.min(100, Math.abs(ev.clientY - cy) * 2 / rect.height * 100)).toFixed(1)
          }
        } else if (isCorner) {
          const d = Math.hypot(ev.clientX - cx, ev.clientY - cy) || 1
          it.size = +Math.max(1, Math.min(20, size0 * (d / d0))).toFixed(2)
        } else if (pos === 'l' || pos === 'r') {
          it.maxW = Math.round(Math.max(20, Math.min(96, Math.abs(ev.clientX - cx) * 2 / rect.width * 100)))
        }
      }, false)
    }
    const up = (ev) => {
      if (ev.pointerId !== pid) return
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  function Handles({ type, item, listName }) {
    const spots = type === 'ov'
      ? ['tl', 'tr', 'bl', 'br', 'l', 'r', 't', 'b']
      : ['tl', 'tr', 'bl', 'br', 'l', 'r']
    return spots.map((pos) => (
      <div key={pos} className={`rs-handle rs-${pos}`}
           onPointerDown={(e) => handleDown(e, pos, type, item, listName)} />
    ))
  }

  // ---- visible items ----
  // captions are source-anchored (REN-115): materialize to timeline words for
  // display; the original group drives selection/editing. hidden tracks (REN-112)
  // don't draw — preview mirrors the export exactly.
  const capViews = (p.captions || []).map((c) => ({ orig: c, mat: materializeCap(p, c) }))
  // clip each group's +0.15s tail at the next group's start, then show at most
  // ONE caption at a time (most-recent-start) so back-to-back karaoke groups
  // never overlap (REN-116) — same formula as render/common.py
  const capBounds = (() => {
    const raw = capViews.map((v) => (v.mat ? { v, sp: capSpan(v.mat) } : null))
      .filter((x) => x && x.sp).map((x) => ({ v: x.v, s0: x.sp[0], s1: x.sp[1] }))
    const starts = raw.map((r) => r.s0).sort((a, b) => a - b)
    return raw.map((r) => {
      const nxt = starts.find((st) => st > r.s0 + 1e-4)
      return { v: r.v, s0: r.s0, end: nxt != null ? Math.min(r.s1, nxt) : r.s1 }
    })
  })()
  const capWinner = (() => {
    const vis = capBounds.filter((b) => time >= b.s0 && time <= b.end).sort((a, b) => a.s0 - b.s0)
    return vis.length ? vis[vis.length - 1].v : null
  })()
  const visCaps = capsHidden ? [] : capViews.filter(({ orig }) =>
    isEditing('cap', orig.id) || (capWinner && capWinner.orig.id === orig.id))
  const visTexts = textsHidden ? [] : (p.texts || []).filter((x) =>
    (time >= x.t0 && time < x.t1) || isEditing('text', x.id))
  // overlays: hide removes only the PICTURE — the <video> stays mounted so its
  // audio keeps playing in the preview, matching the renderer (hide skips the
  // draw, only mute drops audio). Filtering to [] would unmount it and silence
  // audible b-roll that the export still contains.
  const visOverlays = (p.overlays || []).filter((o) => time >= o.t0 && time < (o.t1 ?? dur))

  // zoom keyframes: scale + origin on the video layer
  const zoom = clip ? zoomAt(clip, time - clipStart) : null
  const zoomStyle = zoom && Math.abs(zoom.scale - 1) > 0.001 ? {
    transform: `scale(${zoom.scale})`,
    transformOrigin: `${zoom.cx * 100}% ${zoom.cy * 100}%`,
  } : {}

  // Live retouch approximation (REN-84): a face-MASKED CSS filter on a duplicate
  // source layer — every slider contributes a term so each one visibly responds,
  // scaled by the same master m the export uses; masked to the tracked face so
  // the rest of the frame is untouched. The accurate path is "Process" (real
  // pipeline, swapped in via rtCacheFresh). "compare" hides it.
  const rt = clip?.rt
  const rtLiveOn = !!rt && !compare && !rtCacheFresh(clip) && pick(clip).kind === 'src'
  const rtSrcT = clip ? clip.in + (time - clipStart) : 0
  const faceBox = rtLiveOn ? faceBoxAt(clip, rtSrcT) : null
  function retouchFilter(r) {
    const m = 0.4 + 0.6 * (r.intensity ?? 0) / 100
    const g = (k) => (r[k] ?? 0) / 100 * m
    const blur = (g('smooth') * 1.4).toFixed(2)
    const sat = (1 - 0.18 * g('even') + 0.06 * g('plump')).toFixed(3)
    const bright = (1 + 0.05 * g('plump') + 0.07 * g('eyes') + 0.05 * g('circles') - 0.05 * g('shine')).toFixed(3)
    const contrast = (1 - 0.05 * g('blem') - 0.04 * g('even')).toFixed(3)
    return `blur(${blur}px) saturate(${sat}) brightness(${bright}) contrast(${contrast})`
  }
  const faceMask = faceBox ? (() => {
    const grad = `radial-gradient(ellipse ${(faceBox.w * 62).toFixed(1)}% ${(faceBox.h * 82).toFixed(1)}%` +
      ` at ${(faceBox.x * 100).toFixed(1)}% ${(faceBox.y * 100).toFixed(1)}%, #000 55%, transparent 100%)`
    return { WebkitMaskImage: grad, maskImage: grad }
  })() : null
  const activeSrcObj = clip ? (p.sources?.[clip.src || 'main'] || null) : null

  const veil = clip ? 1 - clipFade(clip, time - clipStart) : 0
  const aspect = p.aspect === '16:9' ? '16/9' : '9/16'
  const stale = !!clip?.bg?.stale
  const bgBadge = !!(clip?.bg?.processed && !clip.bg.stale)
  const pickKind = clip ? pick(clip).kind : 'src'
  const activeSrc = activeSrcObj

  // width slider (REN-81): shown when a text element is selected
  const widthTarget = sel && (sel.type === 'cap' || sel.type === 'text')
    ? (sel.type === 'cap' ? p.captions : p.texts).find((x) => x.id === sel.id)
    : null
  const widthDefault = sel?.type === 'cap' ? 86 : 90
  const [sliderDrag, setSliderDrag] = useState(false)

  function sliderDown(e) {
    if (typeLocked(p, sel?.type)) return // locked track: no width slider
    e.stopPropagation()
    e.preventDefault()
    beginGesture()
    setSliderDrag(true)
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
    const track = e.currentTarget.getBoundingClientRect()
    const apply = (clientY) => {
      const frac = Math.max(0, Math.min(1, (clientY - track.top) / track.height))
      const maxW = Math.round(96 - frac * (96 - 20)) // top = wide, bottom = narrow
      mutate((pp) => {
        const list = sel.type === 'cap' ? pp.captions : pp.texts
        const it = list.find((x) => x.id === sel.id)
        if (it) it.maxW = maxW
      }, false)
    }
    apply(e.clientY)
    const pid = e.pointerId
    const move = (ev) => { if (ev.pointerId === pid) apply(ev.clientY) }
    const up = (ev) => {
      if (ev.pointerId !== pid) return
      setSliderDrag(false)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }

  const stepFrame = (dir) => {
    setPlaying(false)
    timeStore.set(Math.max(0, Math.min(dur, time + dir / 30)))
  }

  return (
    <div className={`player-wrap ${fullscreen ? 'fs' : ''} ${isMobile ? 'mobile' : ''}`}>
      {stale && !fullscreen && (
        <div className="stale-banner">
          ⚠ Clip changed —&nbsp;
          <u onClick={() => { setSel({ type: 'clip', id: clip.id }); processBg(clip.id) }}>re-process background</u>
        </div>
      )}

      {p.clips.length > 0 ? (
        <div className="preview-frame" ref={frameRef} style={{ aspectRatio: aspect }}
             onPointerDown={(e) => { if (e.target === e.currentTarget) setSel(null) }}>
          <div className="video-layer" style={zoomStyle}>
            {Object.entries(p.sources || {}).map(([key, src]) => (
              <video key={key} ref={(el) => { srcRefs.current[key] = el }}
                     src={fileUrl(p, src.proxy || src.path)}
                     playsInline preload="auto"
                     style={{ opacity: 0 }} />
            ))}
            {/* second buffer of each source — pre-seeked to the next clip so a
                cut is a swap, not a seek (REN-137) */}
            {Object.entries(p.sources || {}).map(([key, src]) => (
              <video key={`b-${key}`} ref={(el) => { srcRefsB.current[key] = el }}
                     src={fileUrl(p, src.proxy || src.path)}
                     playsInline preload="auto"
                     style={{ opacity: 0 }} />
            ))}
            {(p.clips || []).filter((c) => c.bg?._cache?.path).map((c) => (
              <video key={c.id} ref={(el) => { cacheRefs.current[c.id] = el }}
                     src={fileUrl(p, c.bg._cache.path)}
                     playsInline preload="auto"
                     style={{ opacity: 0 }} />
            ))}
            {(p.clips || []).filter((c) => c.rt?._cache?.path).map((c) => (
              <video key={c.id} ref={(el) => { rtRefs.current[c.id] = el }}
                     src={fileUrl(p, c.rt._cache.path)}
                     playsInline preload="auto"
                     style={{ opacity: 0 }} />
            ))}
            {/* live face-masked retouch approximation — one duplicate of the
                active source, filtered and clipped to the tracked face */}
            {rtLiveOn && (
              // kept mounted across detection gaps (just hidden) so it never
              // remounts to currentTime 0 and flashes the source's first frame
              <video ref={rtLiveRef} key={`rtlive-${clip.src || 'main'}`}
                     src={fileUrl(p, activeSrcObj?.proxy || activeSrcObj?.path)}
                     playsInline preload="auto" muted
                     style={{ filter: retouchFilter(rt), ...(faceMask || {}), opacity: faceBox ? 1 : 0 }} />
            )}
          </div>

          {/* video track hidden → black frame, other elements still on top */}
          {videoHidden && <div className="video-black" />}

          {/* preview-only UI: vignette + chips (not exported) */}
          <div className="vignette" />
          {clip && (
            <div className="take-chip">
              take {ci + 1} · {pickKind === 'rt' ? 'retouch preview' : pickKind === 'bg' ? 'bg preview'
                : activeSrc?.proxy ? 'proxy 720p' : 'full-res source'}
            </div>
          )}
          {bgBadge && <div className="bg-chip">BG processed</div>}
          {rtLiveOn && faceBox && <div className="rt-chip">◐ retouch preview · live approximation</div>}

          {visOverlays.map((o) => {
            const a = itemAlpha(time, o.t0, o.t1 ?? dur, o.fadeIn, o.fadeOut)
            const isSel = sel?.type === 'ov' && sel.id === o.id
            const url = o.kind === 'image'
              ? fileUrl(p, o.cut && o.cutPath ? o.cutPath : o.path)
              : fileUrl(p, o.proxy || o.path)
            return (
              <div key={o.id}
                   className={`pv-el pv-ov ${isSel ? 'sel' : ''}`}
                   onPointerDown={(e) => elementPointerDown(e, 'ov', o, 'overlays')}
                   style={{
                     left: `${o.x}%`, top: `${o.y}%`, width: `${o.w}%`,
                     height: o.h ? `${(o.h / 100) * frameH}px` : undefined,
                     opacity: ovHidden ? 0 : a,
                     pointerEvents: ovHidden ? 'none' : undefined,
                   }}>
                <div className={`pv-ov-media ${o.kind === 'image' && o.cut ? 'cut' : ''}`}>
                  {o.kind === 'image'
                    ? <img src={url} alt="" draggable={false}
                           style={o.h ? { height: '100%', objectFit: 'cover' } : undefined} />
                    : <video ref={(el) => { ovRefs.current[o.id] = el }} src={url}
                             playsInline preload="auto" muted={!o.vol}
                             style={{
                               height: o.h ? '100%' : undefined,
                               objectPosition: o.crop === 'top' ? 'center top' : 'center',
                             }} />}
                </div>
                {isSel && <Handles type="ov" item={o} listName="overlays" />}
              </div>
            )
          })}

          {visTexts.map((x) => {
            const fc = FONTS_CSS[x.font] || FONTS_CSS['Arial Rounded']
            const a = itemAlpha(time, x.t0, x.t1, x.fadeIn, x.fadeOut)
            const isSel = sel?.type === 'text' && sel.id === x.id
            const editing = isEditing('text', x.id)
            const style = {
              left: `${x.x}%`, top: `${x.y}%`,
              maxWidth: `${x.maxW ?? 90}%`,
              fontSize: (x.size / 100) * frameH,
              fontFamily: fc.family, fontWeight: 700,
              color: x.color, opacity: editing ? 1 : a,
              textShadow: x.shadow !== false ? '0 3px 14px rgba(0,0,0,0.65)' : 'none',
            }
            if (editing) {
              // distinct key: the contentEditable's imperative text nodes are
              // unknown to React — reuse of the DOM node would duplicate them
              return (
                <div key={`${x.id}#edit`} className="pv-el pv-text sel editing" style={style}
                     contentEditable suppressContentEditableWarning spellCheck={false}
                     ref={seedEditable('text', x, x.text)}
                     onInput={editInput('text', x)}
                     onBlur={commitEdit}
                     onKeyDown={editKeyDown('text', x)}
                     onPointerDown={(e) => e.stopPropagation()} />
              )
            }
            return (
              <div key={x.id} className={`pv-el pv-text ${isSel ? 'sel' : ''}`}
                   onPointerDown={(e) => elementPointerDown(e, 'text', x, 'texts')}
                   style={style}>
                {wrapSafe(x.text)}
                {isSel && <Handles type="text" item={x} listName="texts" />}
              </div>
            )
          })}

          {visCaps.map(({ orig: c, mat }) => {
            const fc = FONTS_CSS[c.font] || FONTS_CSS['Helvetica Bold']
            const isSel = sel?.type === 'cap' && sel.id === c.id
            const editing = isEditing('cap', c.id)
            if (editing) {
              return (
                <div key={c.id} className="pv-el pv-cap sel editing"
                     style={{ left: `${c.x}%`, top: `${c.y}%`, width: `${c.maxW ?? 86}%` }}>
                  <div key="edit" contentEditable suppressContentEditableWarning spellCheck={false}
                       className="cap-edit"
                       style={{
                         fontSize: (c.size / 100) * frameH, fontFamily: fc.family,
                         color: c.color, fontWeight: 700, lineHeight: 1.25,
                         textShadow: '0 2px 10px rgba(0,0,0,0.7)',
                       }}
                       ref={seedEditable('cap', c, (c.words || []).map((w) => w.w).join(' '))}
                       onInput={editInput('cap', c)}
                       onBlur={commitEdit}
                       onKeyDown={editKeyDown('cap', c)}
                       onPointerDown={(e) => e.stopPropagation()} />
                </div>
              )
            }
            if (!mat) return null // all words cut out of the timeline
            return (
              <div key={c.id} className={`pv-el pv-cap ${isSel ? 'sel' : ''}`}
                   onPointerDown={(e) => elementPointerDown(e, 'cap', c, 'captions')}
                   style={{ left: `${c.x}%`, top: `${c.y}%`, width: `${c.maxW ?? 86}%` }}>
                <div key="words" className="words"
                     style={{ fontSize: (c.size / 100) * frameH, fontFamily: fc.family }}>
                  {mat.words.map((w, i) => {
                    const spoken = c.mode === 'static' || (time >= w.t && time < w.t + w.d)
                    return (
                      <span key={i} style={{ color: spoken ? c.color : `rgba(255,255,255,${c.dim})` }}>
                        {w.w}
                      </span>
                    )
                  })}
                </div>
                {isSel && <Handles type="cap" item={c} listName="captions" />}
              </div>
            )
          })}

          {veil > 0.001 && <div className="veil" style={{ opacity: veil }} />}

          {/* words-per-line slider (Edits style) — REN-81 */}
          {widthTarget && !inlineEdit && (
            <div className="width-slider" onPointerDown={sliderDown}>
              <div className="track" />
              <div className="knob"
                   style={{ top: `${(96 - (widthTarget.maxW ?? widthDefault)) / (96 - 20) * 100}%` }}>
                {sliderDrag && <span className="val">{widthTarget.maxW ?? widthDefault}%</span>}
              </div>
            </div>
          )}

          {rt && (
            <div className="compare-pill"
                 onPointerDown={(e) => { e.stopPropagation(); e.preventDefault(); setCompare(true) }}
                 onPointerUp={() => setCompare(false)}
                 onPointerLeave={() => setCompare(false)}
                 onPointerCancel={() => setCompare(false)}>
              {compare ? 'original' : '◐ hold to compare'}
            </div>
          )}
        </div>
      ) : (
        <div className="preview-empty" style={{ aspectRatio: aspect }}>
          <b>No source video yet</b>
          <span>Import your raw video to start cutting — or ask Claude to bring one in.</span>
          <button className="btn-import-teal" onClick={onImportVideo}>Import video…</button>
        </div>
      )}

      {(p.audios || []).map((a) => (
        <audio key={a.id} ref={(el) => { audRefs.current[a.id] = el }}
               src={fileUrl(p, a.path)} preload="auto" />
      ))}

      <div className="transport">
        <button className="step" title="Back 1 frame" onClick={() => stepFrame(-1)}>‹|</button>
        <button className="play" title="Play/Pause (space)"
                onClick={() => p.clips.length && setPlaying(!playing)}>{playing ? '❚❚' : '▶'}</button>
        <button className="step" title="Forward 1 frame" onClick={() => stepFrame(1)}>|›</button>
        <div className="time"><b>{fmtTime(time)}</b> / {fmtTime(dur)}</div>
        <button className="step" title="Fullscreen preview (F)"
                onClick={() => setFullscreen(!fullscreen)}>{fullscreen ? '✕' : '⛶'}</button>
      </div>
    </div>
  )
}
