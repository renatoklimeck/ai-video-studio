// Top-level studio state (design/README.md §State Management):
// screen · library · currentId · project · past/future · time · playing ·
// pxPerSec · sel · saveState · modal · fullscreen · importing · vw · menuOpen ·
// compare · export · bgJob · chat · snapshots
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, pollJob } from './api'
import { CAP_MIN, capLimits, capSpans, capTimelineSpan, freeSlotAt, mkWordsSrcCovered, outDuration, outToSource,
         sourceToTimeline, typeLocked, uid } from './time'
import { timeStore } from './timeStore'

const clone = (o) => structuredClone(o)

// Signature of the retouch slider values — a processed preview is stale when
// any of these change (mirrors the bg preview's in/out staleness).
const RT_KEYS = ['v', 'intensity', 'smooth', 'even', 'blem', 'dewrinkle', 'shine', 'plump', 'eyes', 'circles']
const rtSig = (rt) => RT_KEYS.map((k) => rt?.[k] ?? 0).join(',')

// classify a dropped/pasted file by mime then extension (REN-85)
const IMG_RE = /\.(png|jpe?g|gif|webp|heic|heif|bmp|avif)$/i
const VID_RE = /\.(mp4|mov|m4v|webm|avi|mkv)$/i
const AUD_RE = /\.(mp3|wav|m4a|aac|ogg|oga|flac|aiff?)$/i
export function classifyFile(file) {
  const n = file?.name || '', t = file?.type || ''
  if (t.startsWith('video/') || VID_RE.test(n)) return 'video'
  if (t.startsWith('image/') || IMG_RE.test(n)) return 'image'
  if (t.startsWith('audio/') || AUD_RE.test(n)) return 'audio'
  return null
}

// Undo/redo restore whole past snapshots, but derived media adopted since
// (proxies, filmstrips, waveforms — mutate(fn, false), never in history) must
// survive the restore, or a Cmd+Z after a heal silently drops them and the
// autosave persists the loss (full ffmpeg regeneration on next open).
function graftDerived(restored, cur) {
  if (!restored || !cur) return restored
  for (const [k, src] of Object.entries(cur.sources || {})) {
    const r = restored.sources?.[k]
    if (!r) continue
    if (!r.proxy && src.proxy) r.proxy = src.proxy
    if (!r.strip && src.strip) {
      r.strip = src.strip; r.stripInterval = src.stripInterval; r.stripFrames = src.stripFrames
    }
    if (!r.vwave && src.vwave) r.vwave = src.vwave
  }
  for (const o of cur.overlays || []) {
    const r = (restored.overlays || []).find((x) => x.id === o.id)
    if (!r) continue
    if (!r.proxy && o.proxy) r.proxy = o.proxy
    if (!r.cutPath && o.cutPath) r.cutPath = o.cutPath
    if (!r.strip && o.strip) {
      r.strip = o.strip; r.stripInterval = o.stripInterval; r.stripFrames = o.stripFrames
    }
  }
  for (const a of cur.audios || []) {
    const r = (restored.audios || []).find((x) => x.id === a.id)
    if (r && !r.wave && a.wave) { r.wave = a.wave; r.dur = a.dur }
  }
  // the script + its approval (REN-123/127) are edited out-of-history (no undo
  // entry), so keep the current ones across undo/redo/restore.
  if (cur.script != null) restored.script = cur.script
  if (cur.scriptApproved != null) restored.scriptApproved = cur.scriptApproved
  return restored
}

export function useStudio() {
  const [screen, setScreen] = useState('projects')
  const [library, setLibrary] = useState([])
  const [currentId, setCurrentId] = useState(null)
  const [project, setProject] = useState(null)
  const [playing, setPlaying] = useState(false)
  const [pxPerSec, setPxPerSec] = useState(44)
  const [sel, setSel] = useState(null)
  const [saveState, setSaveState] = useState('saved')
  const [modal, setModal] = useState(null)           // null | 'export' | 'history'
  const [fullscreen, setFullscreen] = useState(false)
  const [importing, setImporting] = useState(false)
  const [vw, setVw] = useState(window.innerWidth || 1280)
  const [menuOpen, setMenuOpen] = useState(false)
  const [compare, setCompare] = useState(false)
  const [inlineEdit, setInlineEdit] = useState(null)  // {type:'cap'|'text', id} | null
  const [exp, setExp] = useState({ phase: 'choose', pct: 0, quality: 'final', out: null, filename: null, log: [] })
  const [transc, setTransc] = useState({ phase: 'choose', pct: 0, added: null, log: [] })
  const [bgJob, setBgJob] = useState(null)            // clip id being processed
  const [bgJobPct, setBgJobPct] = useState(0)
  const [bgJobAll, setBgJobAll] = useState(null)      // {done,total} while doing every clip
  const [rtJob, setRtJob] = useState(null)            // clip id: retouch preview
  const [rtJobPct, setRtJobPct] = useState(0)
  const [faceTracks, setFaceTracks] = useState({})    // srcKey -> track (runtime only)
  const [toast, setToast] = useState(null)            // transient error/info toast
  const [appVersion, setAppVersion] = useState('')    // release name, e.g. 2026.08.04.1
  const [busyMedia, setBusyMedia] = useState([])      // ids uploading/processing (chips)
  const [leftTab, setLeftTab] = useState('claude')    // 'claude' | 'transcript'
  const [transcripts, setTranscripts] = useState({})  // srcKey -> {words, lang} (runtime)
  const [transcribing, setTranscribing] = useState([]) // srcKeys with a running job
  const [chatOpen, setChatOpen] = useState(window.innerWidth >= 760)
  const [chatMsgs, setChatMsgs] = useState([])
  const [chatInput, setChatInput] = useState('')
  const [claudeTyping, setClaudeTyping] = useState(false)
  // Which Claude model + reasoning effort the chat uses (REN-118); remembered
  // per-user in localStorage. Keys map to real --model ids on the server.
  // guard against a stale saved value (e.g. a model we removed) → valid default
  const [chatModel, setChatModelState] = useState(() => {
    return localStorage.getItem('vs-chat-model') || 'claude:claude-opus-5'
  })
  // which AI engines this machine can run (claude / codex-ChatGPT, REN-130)
  const [engines, setEngines] = useState({ claude: true, codex: false })
  // models the picker offers — discovered from the installed CLIs (REN-136)
  const [models, setModels] = useState([])
  const loadModels = useCallback(() => {
    api.getModels().then((r) => {
      setModels(r.models || [])
      setChatModelState((prev) => {
        const keys = (r.models || []).map((m) => m.key)
        if (keys.includes(prev)) return prev
        // migrate a legacy short key ('opus') or an unavailable one
        const legacy = { opus: 'claude:claude-opus-4-8', sonnet: 'claude:claude-sonnet-5',
                         fable: 'claude:claude-fable-5', codex: 'codex:' }[prev]
        const next = (legacy && keys.includes(legacy) && legacy)
          || (keys.includes(r.default) && r.default) || keys[0] || prev
        if (next !== prev) localStorage.setItem('vs-chat-model', next)
        return next
      })
    }).catch(() => {})
  }, [])
  const rescanModels = useCallback(async () => {
    await api.rescanModels()
    setTimeout(loadModels, 4000)
  }, [loadModels])
  const [chatEffort, setChatEffortState] = useState(() => {
    const e = localStorage.getItem('vs-chat-effort')
    return ['low', 'medium', 'high', 'max', 'ultracode'].includes(e) ? e : 'medium'
  })
  const setChatModel = useCallback((m) => { setChatModelState(m); localStorage.setItem('vs-chat-model', m) }, [])
  const setChatEffort = useCallback((e) => { setChatEffortState(e); localStorage.setItem('vs-chat-effort', e) }, [])
  // one-click chat presets (REN-122), user-editable
  const [presets, setPresets] = useState([])
  const [editingPreset, setEditingPreset] = useState(null) // preset being edited, or {} for new
  const loadPresets = useCallback(() => { api.getPresets().then((r) => setPresets(r.presets || [])).catch(() => {}) }, [])
  const savePresets = useCallback(async (list) => { const r = await api.putPresets(list); setPresets(r.presets || []); return r.presets }, [])
  const [snapshots, setSnapshots] = useState([])
  const [histStamp, setHistStamp] = useState(0)       // bump = undo/redo stacks changed

  const pastRef = useRef([])
  const healingRef = useRef(new Set())
  // transcribeSources is defined further down; the take importers call it
  // through this ref (REN-133 auto-transcribe on import)
  const transcribeRef = useRef(null)
  const futureRef = useRef([])
  const pendingGestureRef = useRef(null) // staged undo snapshot, committed on first mutation
  const projRef = useRef(null)
  const pidRef = useRef(null)
  const selRef = useRef(null)
  const saveTimer = useRef(null)
  const mtimeRef = useRef(0)
  const savingRef = useRef(false)
  const typingTimer = useRef(null)
  const toastTimer = useRef(null)
  const screenRef = useRef(screen)
  projRef.current = project
  pidRef.current = currentId
  selRef.current = sel
  screenRef.current = screen

  // ---------- library ----------
  const loadLibrary = useCallback(async () => {
    try { setLibrary(await api.projects()) } catch { setLibrary([]) }
  }, [])

  useEffect(() => { loadLibrary() }, [loadLibrary])
  useEffect(() => { loadPresets() }, [loadPresets])
  useEffect(() => { loadModels() }, [loadModels])
  useEffect(() => {
    // engines fetch with retry (a codex-only student must not get stuck on a
    // flaky first fetch), then auto-switch the model if its engine is missing
    // (e.g. saved 'opus' on a machine that only has codex → 'codex').
    let dead = false
    const load = (attempt = 0) => {
      api.getEngines().then((r) => {
        if (dead) return
        setEngines(r)
        setChatModelState((prev) => {
          const eng = prev === 'codex' ? 'codex' : 'claude'
          if (r[eng] !== false) return prev
          const fallback = eng === 'claude' && r.codex ? 'codex'
            : eng === 'codex' && r.claude ? 'opus' : prev
          if (fallback !== prev) localStorage.setItem('vs-chat-model', fallback)
          return fallback
        })
      }).catch(() => { if (!dead && attempt < 3) setTimeout(() => load(attempt + 1), 2000) })
    }
    load()
    return () => { dead = true }
  }, [])
  useEffect(() => {
    const onResize = () => { if (window.innerWidth > 0) setVw(window.innerWidth) }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const refreshHistory = useCallback(async (pid) => {
    try { setSnapshots(await api.history(pid ?? pidRef.current)) } catch { /* keep */ }
  }, [])

  // ---------- auto-save (900ms debounce; no Save button anywhere) ----------
  const doSave = useCallback(async () => {
    const p = projRef.current
    if (!p || !pidRef.current) return
    savingRef.current = true
    try {
      const r = await api.save(pidRef.current, p)
      mtimeRef.current = r.mtime
      setSaveState('saved')
      refreshHistory()
    } catch {
      setSaveState('saving') // keep amber; next mutation retries
    } finally {
      savingRef.current = false
    }
  }, [refreshHistory])

  const scheduleSave = useCallback(() => {
    setSaveState('saving')
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      saveTimer.current = null
      doSave()
    }, 900)
  }, [doSave])

  // Flush any pending debounced save NOW (before restore / navigation / jobs)
  const flushSave = useCallback(async () => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current)
      saveTimer.current = null
    }
    await doSave()
  }, [doSave])

  // auto-update (REN-131): the standalone app once kept a days-old cached UI.
  // Poll the served build id; when it changes, flush any pending save and
  // reload so the UI always matches the server.
  useEffect(() => {
    let base = null, dead = false
    const check = async () => {
      try {
        const { build, version } = await api.version()
        if (dead) return
        // the release name for the header (REN-168) — same request, no extra poll
        if (version) setAppVersion(version)
        if (!build) return
        if (base == null) { base = build; return }
        if (build !== base) { await flushSave(); window.location.reload() }
      } catch { /* server briefly down — next tick retries */ }
    }
    const iv = setInterval(check, 60000)
    window.addEventListener('focus', check)
    check()
    return () => { dead = true; clearInterval(iv); window.removeEventListener('focus', check) }
  }, [flushSave])

  const openProject = useCallback(async (id, preloaded) => {
    if (saveTimer.current && pidRef.current) await flushSave()
    const p = preloaded || await api.get(id)
    pastRef.current = []
    futureRef.current = []
    pendingGestureRef.current = null
    mtimeRef.current = p._mtime || 0
    seenScriptRef.current = (p.script || '').trim()   // already on screen, not news
    setScriptAlert(null)      // never carry A's script into B (approving would write it there)
    setCurrentId(id)
    setProject(p)
    setScreen('editor')
    timeStore.set(0)
    setPlaying(false)
    setSel(null)
    setInlineEdit(null)
    // runtime caches are keyed by bare source key ('main' collides across every
    // project) — clear them or project B would show project A's transcript/track
    setTranscripts({}); setTranscribing([]); transcriptKeys.current.clear()
    setFaceTracks({}); faceTrackKeys.current.clear()
    setSaveState('saved')
    setChatOpen(window.innerWidth >= 760)
    setExp({ phase: 'choose', pct: 0, quality: 'final', out: null, filename: null, log: [] })
    setHistStamp((s) => s + 1)
    refreshHistory(id)
    api.chat(id).then(setChatMsgs).catch(() => setChatMsgs([]))
    healMedia(id, p)
  }, [refreshHistory])

  // self-heal timeline media: 720p proxies (smooth playback), filmstrips for
  // the timeline (REN-82) and audio waveforms — generated in the background
  // whenever a project is missing them
  const healMedia = useCallback((id, proj) => {
    // proj is passed explicitly on open (refs only update after the commit)
    const p = proj || projRef.current
    if (!p) return
    const guard = (key) => {
      const k = `${id}:${key}`
      if (healingRef.current.has(k)) return true
      healingRef.current.add(k)
      return false
    }
    for (const [key, src] of Object.entries(p.sources || {})) {
      if (!src.path) continue
      if (!src.proxy && !guard(`proxy-${key}`)) {
        api.proxy(id, src.path).then(({ job, path }) =>
          pollJob(job).then((st) => {
            healingRef.current.delete(`${id}:proxy-${key}`)
            if (st.status !== 'done' || pidRef.current !== id) return
            mutate((pp) => { if (pp.sources?.[key]) pp.sources[key].proxy = path }, false)
            healMedia(id) // now build the strip from the proxy
          })
        ).catch(() => healingRef.current.delete(`${id}:proxy-${key}`))
      }
      if (src.proxy && !src.strip && !guard(`strip-${key}`)) {
        api.strip(id, src.proxy).then(({ job, path, interval, frames }) =>
          pollJob(job).then((st) => {
            healingRef.current.delete(`${id}:strip-${key}`)
            if (st.status !== 'done' || pidRef.current !== id) return
            mutate((pp) => {
              const s2 = pp.sources?.[key]
              if (s2) { s2.strip = path; s2.stripInterval = interval; s2.stripFrames = frames }
            }, false)
          })
        ).catch(() => healingRef.current.delete(`${id}:strip-${key}`))
      }
      // audio waveform for the video track (REN-114) — cyan wave under the strip
      if (!src.vwave && !guard(`vwave-${key}`)) {
        api.srcwave(id, src.path).then(({ job, path }) =>
          pollJob(job).then((st) => {
            healingRef.current.delete(`${id}:vwave-${key}`)
            if (st.status !== 'done' || pidRef.current !== id) return
            mutate((pp) => { if (pp.sources?.[key]) pp.sources[key].vwave = path }, false)
          })
        ).catch(() => healingRef.current.delete(`${id}:vwave-${key}`))
      }
    }
    for (const o of p.overlays || []) {
      if (o.kind !== 'video' || o.strip || !(o.proxy || o.path)) continue
      if (guard(`ovstrip-${o.id}`)) continue
      api.strip(id, o.proxy || o.path).then(({ job, path, interval, frames }) =>
        pollJob(job).then((st) => {
          healingRef.current.delete(`${id}:ovstrip-${o.id}`)
          if (st.status !== 'done' || pidRef.current !== id) return
          mutate((pp) => {
            const o2 = pp.overlays.find((x) => x.id === o.id)
            if (o2) { o2.strip = path; o2.stripInterval = interval; o2.stripFrames = frames }
          }, false)
        })
      ).catch(() => healingRef.current.delete(`${id}:ovstrip-${o.id}`))
    }
    for (const a of p.audios || []) {
      if (a.wave || !a.path || guard(`wave-${a.id}`)) continue
      api.wave(id, a.path).then(({ job, path, duration }) =>
        pollJob(job).then((st) => {
          healingRef.current.delete(`${id}:wave-${a.id}`)
          if (st.status !== 'done' || pidRef.current !== id) return
          mutate((pp) => {
            const a2 = pp.audios.find((x) => x.id === a.id)
            if (a2) { a2.wave = path; a2.dur = duration }
          }, false)
        })
      ).catch(() => healingRef.current.delete(`${id}:wave-${a.id}`))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const backToProjects = useCallback(async () => {
    setScreen('projects')
    setScriptAlert(null)
    setPlaying(false)
    setFullscreen(false)
    setSel(null)
    setMenuOpen(false)
    if (saveTimer.current) await flushSave()
    loadLibrary()
  }, [loadLibrary, flushSave])

  // Reload when project.json changes on disk under our feet (Claude edits it)
  useEffect(() => {
    if (screen !== 'editor' || !currentId) return
    const iv = setInterval(async () => {
      if (savingRef.current || saveTimer.current && saveState === 'saving') return
      try {
        const { mtime } = await api.mtime(currentId)
        if (mtime !== mtimeRef.current && saveState === 'saved') {
          const p = await api.get(currentId)
          mtimeRef.current = p._mtime
          setProject(p)
          noteServerScript(p)
          refreshHistory()
        }
      } catch { /* server restart etc. */ }
    }, 3000)
    return () => clearInterval(iv)
  }, [screen, currentId, saveState, refreshHistory])

  // ---------- mutations / undo ----------
  const mutate = useCallback((fn, undoable = true) => {
    const cur = projRef.current
    if (!cur) return
    if (undoable) {
      pendingGestureRef.current = null
      pastRef.current = [...pastRef.current.slice(-39), clone(cur)]
      futureRef.current = []
      setHistStamp((s) => s + 1)
    } else if (pendingGestureRef.current) {
      // first streamed update of a gesture — commit the staged snapshot
      pastRef.current = [...pastRef.current.slice(-39), pendingGestureRef.current]
      pendingGestureRef.current = null
      futureRef.current = []
      setHistStamp((s) => s + 1)
    }
    const next = clone(cur)
    fn(next)
    // callers (heal chains, gesture streams) read projRef synchronously after
    // mutate — React 18 batches the render, so the ref must update eagerly
    projRef.current = next
    setProject(next)
    scheduleSave()
  }, [scheduleSave])

  const showToast = useCallback((msg) => {
    setToast(msg)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 2800)
  }, [])

  // Apply a clip-structure change and re-anchor captions in the SAME entry, so
  // Captions are now source-anchored (REN-115) — invariant to trim/split/delete,
  // their timeline position is derived at draw time. So a clip mutation needs no
  // caption re-sync; mutateClips is kept as a thin alias for the callers.
  const mutateClips = useCallback((fn, undoable = true) => {
    mutate(fn, undoable)
  }, [mutate])

  // One history entry per continuous gesture (drag/trim/focus): STAGE the
  // snapshot here; mutate(fn, false) commits it on the first real change.
  // A gesture that never mutates (tap on a handle, focus without typing)
  // leaves no history entry and keeps the redo stack intact.
  const beginGesture = useCallback(() => {
    const cur = projRef.current
    if (!cur) return
    pendingGestureRef.current = clone(cur)
  }, [])

  const undo = useCallback(() => {
    pendingGestureRef.current = null
    const prev = pastRef.current.pop()
    if (!prev) return
    futureRef.current = [clone(projRef.current), ...futureRef.current]
    graftDerived(prev, projRef.current)
    projRef.current = prev
    setProject(prev)
    setHistStamp((s) => s + 1)
    scheduleSave()
  }, [scheduleSave])

  const redo = useCallback(() => {
    pendingGestureRef.current = null
    const next = futureRef.current.shift()
    if (!next) return
    pastRef.current = [...pastRef.current.slice(-39), clone(projRef.current)]
    graftDerived(next, projRef.current)
    projRef.current = next
    setProject(next)
    setHistStamp((s) => s + 1)
    scheduleSave()
  }, [scheduleSave])

  const canUndo = pastRef.current.length > 0
  const canRedo = futureRef.current.length > 0

  // ---------- track controls (REN-112) ----------
  // mute/hide/lock per track; undoable (they change the exported output).
  const toggleTrack = useCallback((trackKey, flag) => {
    mutate((p) => {
      if (!p.tracks) p.tracks = {}
      if (!p.tracks[trackKey]) p.tracks[trackKey] = { muted: false, hidden: false, locked: false }
      p.tracks[trackKey][flag] = !p.tracks[trackKey][flag]
    })
  }, [mutate])

  // A track's NAME is content, not a view preference: it belongs in the project
  // so it survives a reload and travels with an exported project. Empty clears
  // it back to the built-in label.
  const renameTrack = useCallback((trackKey, name) => {
    mutate((p) => {
      if (!p.tracks) p.tracks = {}
      if (!p.tracks[trackKey]) p.tracks[trackKey] = { muted: false, hidden: false, locked: false }
      const t = (name || '').trim().slice(0, 40)
      if (t) p.tracks[trackKey].name = t
      else delete p.tracks[trackKey].name
    })
  }, [mutate])

  // ---------- selection ----------
  const deleteSel = useCallback(() => {
    const s = selRef.current
    if (!s) return
    if (typeLocked(projRef.current, s.type)) { showToast('Track is locked'); return }
    mutate((p) => {
      const map = { clip: 'clips', cap: 'captions', text: 'texts', ov: 'overlays', aud: 'audios' }
      p[map[s.type]] = p[map[s.type]].filter((x) => x.id !== s.id)
    })
    setSel(null)
  }, [mutate, showToast])

  // ---------- add elements (created at the playhead, auto-selected) ----------
  const addCaption = useCallback(() => {
    const proj = projRef.current
    if (!proj) return
    const id = uid('g')
    // Land in the next EMPTY stretch at or after the playhead (REN-156), never
    // on top of a caption that is already there. He adds captions with the
    // playhead wherever he last looked, which is usually inside one.
    // Prefer the next empty stretch (REN-156), but NEVER refuse (REN-172).
    // I read the first issue too literally: on a video captioned word by word
    // there is no gap at all, so "+ Caption" turned into a button that only
    // ever said no. Landing on top of another caption is something he can see
    // and fix in a second — a button that does nothing is not.
    const want = 1.2
    const at = +timeStore.get().toFixed(2)
    const slot = freeSlotAt(proj, at, null, 0.6)
    const t = slot ? slot[0] : at
    const dur = slot ? Math.min(want, slot[1] - slot[0]) : want
    if (!slot) {
      showToast('No gap here, so it went in on top — drag it or shorten a neighbour to separate them')
    }
    // anchor the new caption to the source under that instant (REN-115)
    const [ci, srcT] = outToSource(proj, t)
    const src = ci != null ? (proj.clips[ci].src || 'main') : 'main'
    const s0 = ci != null ? srcT : 0
    mutate((p) => {
      p.captions.push({ id, x: 50, y: 76, size: 3.4, font: 'Helvetica Bold', color: '#FFFFFF',
                        dim: 0.45, mode: 'karaoke', capAnchor: 'source', src,
                        words: mkWordsSrcCovered(p, src, 'new caption here', s0, s0 + dur) })
    })
    setSel({ type: 'cap', id })
    timeStore.set(t)
  }, [mutate, showToast])

  // ── one caption's LOOK is the whole video's look (REN-155) ────────────────
  //
  // Inverts the old default. Captions are a style, not eight separate
  // decisions: he moves one up and means "captions sit here". The toggle is ON
  // by default and remembered, so he never has to re-arm it; turning it off
  // makes the next change apply to the selected caption alone.
  //
  // WORDS and TIMES are never spread — those are what each caption IS.
  const CAP_LOOK = ['x', 'y', 'size', 'font', 'color', 'dim', 'mode', 'maxW', 'shadow', 'stroke', 'bg']
  const [capApplyAll, setCapApplyAllState] = useState(
    () => localStorage.getItem('vs-cap-all') !== '0')
  const setCapApplyAll = useCallback((on) => {
    setCapApplyAllState(on)
    try { localStorage.setItem('vs-cap-all', on ? '1' : '0') } catch { /* private mode */ }
  }, [])
  const spreadCapLook = useCallback((p, from) => {
    for (const o of p.captions || []) {
      if (o.id === from.id) continue
      for (const k of CAP_LOOK) {
        if (k in from) o[k] = structuredClone(from[k])
        else delete o[k]
      }
    }
  }, [])

  // ── Enter splits a caption in two (REN-158) ───────────────────────────────
  //
  // The cut in TIME falls between the last word of the first half and the first
  // word of the second, because each half keeps the word timings it already
  // had. Splitting the duration in half instead would desync both halves from
  // the audio. One mutate call, so one ⌘Z undoes the whole split.
  const splitCaption = useCallback((capId, wordIndex) => {
    if (typeLocked(projRef.current, 'cap')) { showToast('Captions track is locked'); return }
    let made = null
    mutate((p) => {
      const i = p.captions.findIndex((c) => c.id === capId)
      if (i < 0) return
      const cap = p.captions[i]
      const ws = cap.words || []
      if (ws.length < 2) return
      const k = Math.max(1, Math.min(Math.round(wordIndex), ws.length - 1))
      const a = { ...structuredClone(cap), words: ws.slice(0, k), edited: true }
      const b = { ...structuredClone(cap), id: uid('g'), words: ws.slice(k), edited: true }
      p.captions.splice(i, 1, a, b)
      made = b.id
    })
    if (made) setSel({ type: 'cap', id: made })
    else showToast('Put the cursor between two words to split the caption')
  }, [mutate, showToast])

  // ── move / stretch a caption on the timeline (REN-157) ────────────────────
  //
  // The words live in SOURCE seconds, so a timeline move becomes a shift of
  // every word by the same amount — the karaoke spacing between them is
  // untouched, only where the group sits changes. It stops at the neighbours
  // instead of pushing them (REN-156: nothing may ever overlap).
  const moveCaption = useCallback((capId, newStart, edge = null) => {
    if (typeLocked(projRef.current, 'cap')) { showToast('Captions track is locked'); return }
    mutate((p) => {
      const cap = p.captions.find((c) => c.id === capId)
      if (!cap || cap.capAnchor !== 'source') return
      const span = capTimelineSpan(p, cap)
      if (!span) return
      const [lo, hi] = capLimits(p, capId, (span[0] + span[1]) / 2)
      const ws = cap.words || []
      if (!ws.length) return
      const s0 = ws[0].t0, s1 = ws[ws.length - 1].t1
      const MIN = CAP_MIN
      // Snapshot, then CHECK the result rather than trusting the limits: the
      // neighbour bounds only look at the two captions either side, and a long
      // drag jumps over a whole run of them, flattening one in the middle. The
      // rule is about the finished state, so it is enforced on the finished
      // state — the move is refused if any caption would end up unreadable.
      const before = ws.map((w) => ({ t0: w.t0, t1: w.t1 }))
      const revert = () => ws.forEach((w, i) => { w.t0 = before[i].t0; w.t1 = before[i].t1 })
      // every OTHER caption must stay readable; this one may be short if it
      // already was
      const wasTiny = new Set(capSpans(p).filter((sp) => sp.b - sp.a < MIN - 1e-3).map((sp) => sp.id))
      const legal = () => capSpans(p).every((sp) =>
        sp.b - sp.a >= MIN - 1e-3 || wasTiny.has(sp.id))

      if (edge === null) {                       // drag the whole group
        const len = span[1] - span[0]
        const want = Math.max(lo, Math.min(newStart, hi - len))
        const [ci, srcT] = outToSource(p, want)
        if (ci == null || (p.clips[ci].src || 'main') !== (cap.src || 'main')) return
        const shift = srcT - s0
        for (const w of ws) { w.t0 = +(w.t0 + shift).toFixed(3); w.t1 = +(w.t1 + shift).toFixed(3) }
        if (!legal()) revert()
        return
      }
      // stretch one edge: scale the words inside the new span so the karaoke
      // keeps its proportions
      const [ci, srcT] = outToSource(p, Math.max(lo, Math.min(newStart, hi)))
      if (ci == null || (p.clips[ci].src || 'main') !== (cap.src || 'main')) return
      let a = s0, b = s1
      if (edge === 'l') a = Math.min(srcT, s1 - MIN)
      else b = Math.max(srcT, s0 + MIN)
      const oldLen = Math.max(1e-3, s1 - s0), newLen = Math.max(MIN, b - a)
      const k = newLen / oldLen
      for (const w of ws) {
        w.t0 = +(a + (w.t0 - s0) * k).toFixed(3)
        w.t1 = +(a + (w.t1 - s0) * k).toFixed(3)
      }
      if (!legal()) revert()
    })
  }, [mutate, showToast])

  const addText = useCallback(() => {
    const t = +timeStore.get().toFixed(1)
    const id = uid('t')
    mutate((p) => {
      p.texts.push({ id, text: 'NEW HEADLINE', x: 50, y: 20, size: 4.2, color: '#FFFFFF',
                     t0: t, t1: +(t + 3).toFixed(1), fadeIn: 0.2, fadeOut: 0.2, shadow: true,
                     font: 'Arial Rounded' })
    })
    setSel({ type: 'text', id })
  }, [mutate])

  const addOverlayFile = useCallback(async (file) => {
    const pid = pidRef.current
    const isVideo = /\.(mp4|mov|m4v|webm)$/i.test(file.name)
    const { path } = await api.upload(pid, file)
    const t = +timeStore.get().toFixed(1)
    const id = uid('o')
    mutate((p) => {
      p.overlays.push({ id, kind: isVideo ? 'video' : 'image', name: file.name, path, proxy: null,
                        cutPath: null, x: 50, y: 40, w: 50, h: null,
                        t0: t, t1: +(t + 4).toFixed(1), fadeIn: 0.2, fadeOut: 0.2,
                        cut: false, vol: 0, crop: 'center' })
    })
    setSel({ type: 'ov', id })
    if (isVideo) {
      const { job, path: pp } = await api.proxy(pid, path)
      pollJob(job).then((st) => {
        if (st.status !== 'done' || pidRef.current !== pid) return
        mutate((p) => {
          const o = p.overlays.find((x) => x.id === id)
          if (o) o.proxy = pp
        }, false)
        healMedia(pid) // timeline mini-strip for the new video overlay
      })
    }
  }, [mutate, healMedia])

  const addAudioFile = useCallback(async (file) => {
    const pid = pidRef.current
    const { path } = await api.upload(pid, file)
    const id = uid('a')
    const dur = outDuration(projRef.current)
    mutate((p) => {
      p.audios.push({ id, name: file.name, path, t0: 0, t1: +Math.max(dur, 4).toFixed(1),
                      vol: 0.5, fadeIn: 0.5, fadeOut: 0.5 })
    })
    setSel({ type: 'aud', id })
    healMedia(pid)
  }, [mutate, healMedia])

  // + Video: import a source file as a NEW take (appends a clip)
  const addTakeFile = useCallback(async (file) => {
    const pid = pidRef.current
    setImporting(true)
    try {
      const r = await api.importTake(pid, file)
      const id = uid('c')
      mutate((p) => {
        p.sources[r.key] = { path: r.path, proxy: null, duration: r.duration }
        if (!p.clips.length && r.w && r.h) {
          p.w = r.w; p.h = r.h
          p.aspect = r.w >= r.h ? '16:9' : '9:16'
        }
        const d = Math.max(r.duration || 12, 2)  // whole take on the timeline
        p.clips.push({ id, src: r.key, in: 0, out: +d.toFixed(3),
                       fadeIn: 0, fadeOut: 0, kfs: [], bg: null, rt: null })
      })
      setSel({ type: 'clip', id })
      transcribeRef.current?.() // background transcript for the new take (REN-133)
      pollJob(r.proxyJob).then((st) => {
        if (st.status === 'done') {
          mutate((p) => { if (p.sources[r.key]) p.sources[r.key].proxy = r.proxy }, false)
          healMedia(pidRef.current)
        }
      })
    } finally {
      setImporting(false)
    }
  }, [mutate, healMedia])

  // ---------- drag & drop / paste media (REN-85) ----------
  // Each adds with mutate(...,false); a whole drop is wrapped in one
  // beginGesture() by dropMedia so the batch is a single undo entry.
  const addTakeAt = useCallback(async (file, insertIndex) => {
    const pid = pidRef.current
    setImporting(true)
    try {
      const r = await api.importTake(pid, file)
      const id = uid('c')
      setBusyMedia((b) => [...b, id])
      mutateClips((p) => {
        p.sources[r.key] = { path: r.path, proxy: null, duration: r.duration }
        if (!p.clips.length && r.w && r.h) { p.w = r.w; p.h = r.h; p.aspect = r.w >= r.h ? '16:9' : '9:16' }
        const d = Math.max(r.duration || 12, 2)  // whole take on the timeline
        const clip = { id, src: r.key, in: 0, out: +d.toFixed(3),
                       fadeIn: 0, fadeOut: 0, kfs: [], bg: null, rt: null }
        const at = Math.max(0, Math.min(insertIndex ?? p.clips.length, p.clips.length))
        p.clips.splice(at, 0, clip)
      }, false)
      setSel({ type: 'clip', id })
      transcribeRef.current?.() // background transcript for the new take (REN-133)
      pollJob(r.proxyJob).then((st) => {
        setBusyMedia((b) => b.filter((x) => x !== id))
        if (st.status === 'done' && pidRef.current === pid) {
          mutate((p) => { if (p.sources[r.key]) p.sources[r.key].proxy = r.proxy }, false)
          healMedia(pidRef.current)
        }
      })
    } finally { setImporting(false) }
  }, [mutate, mutateClips, healMedia])

  const addOverlayAt = useCallback(async (file, kind, pos) => {
    const pid = pidRef.current
    const { path } = await api.upload(pid, file)
    const id = uid('o')
    const t0 = pos.t0 ?? +timeStore.get().toFixed(1)
    mutate((p) => {
      p.overlays.push({ id, kind, name: file.name, path, proxy: null, cutPath: null,
        x: +(Math.max(4, Math.min(96, pos.x ?? 50))).toFixed(1),
        y: +(Math.max(4, Math.min(96, pos.y ?? 40))).toFixed(1),
        w: pos.w ?? 50, h: null, t0: +t0.toFixed(1),
        t1: +(t0 + (kind === 'image' ? 5 : 4)).toFixed(1),
        fadeIn: 0.2, fadeOut: 0.2, cut: false, vol: 0, crop: 'center' })
    }, false)
    setSel({ type: 'ov', id })
    if (kind === 'video') {
      setBusyMedia((b) => [...b, id])
      const { job, path: pp } = await api.proxy(pid, path)
      pollJob(job).then((st) => {
        setBusyMedia((b) => b.filter((x) => x !== id))
        if (st.status !== 'done' || pidRef.current !== pid) return
        mutate((p) => { const o = p.overlays.find((x) => x.id === id); if (o) o.proxy = pp }, false)
        healMedia(pid)
      })
    }
  }, [mutate, healMedia])

  const addAudioAt = useCallback(async (file, t0) => {
    const pid = pidRef.current
    const { path } = await api.upload(pid, file)
    const id = uid('a')
    const dur = outDuration(projRef.current)
    const start = t0 ?? 0
    mutate((p) => {
      p.audios.push({ id, name: file.name, path, t0: +start.toFixed(1),
        t1: +Math.max(start + 4, dur).toFixed(1), vol: 0.5, fadeIn: 0.5, fadeOut: 0.5 })
    }, false)
    setSel({ type: 'aud', id })
    healMedia(pid)
  }, [mutate, healMedia])

  // ---------- clip ops ----------
  const setAspect = useCallback((a) => {
    mutate((p) => {
      if (p.aspect === a) return
      p.aspect = a
      const big = Math.max(p.w, p.h), small = Math.min(p.w, p.h)
      if (a === '9:16') { p.w = small; p.h = big } else { p.w = big; p.h = small }
    })
  }, [mutate])

  const splitClip = useCallback(() => {
    const p = projRef.current
    const t = timeStore.get()
    const [ci] = outToSource(p, t)
    if (ci == null) return
    const clip = p.clips[ci]
    const start = p.clips.slice(0, ci).reduce((a, c) => a + (c.out - c.in), 0)
    const at = clip.in + (t - start)
    if (at <= clip.in + 0.2 || at >= clip.out - 0.2) return
    mutate((pp) => {
      const i = pp.clips.findIndex((c) => c.id === clip.id)
      const a = clone(pp.clips[i])
      const b = clone(pp.clips[i])
      a.out = +at.toFixed(2); a.fadeOut = 0
      b.in = +at.toFixed(2); b.fadeIn = 0; b.id = uid('c'); b.kfs = []
      if (a.bg?.processed) { a.bg.stale = true }
      if (b.bg) { b.bg.stale = !!b.bg.processed; delete b.bg._cache }
      pp.clips.splice(i, 1, a, b)
    })
  }, [mutate])

  // ---------- background removal job ----------
  const processBg = useCallback(async (clipId) => {
    const pid = pidRef.current
    await flushSave()
    // the job renders the clip range as saved NOW — remember it, so edits made
    // while the job runs correctly leave the cache marked stale
    const at = projRef.current?.clips.find((x) => x.id === clipId)
    if (!at) return
    const jobIn = at.in, jobOut = at.out
    setBgJob(clipId)
    setBgJobPct(0)
    try {
      const { job } = await api.segbg(pid, clipId)
      const st = await pollJob(job, (s) => setBgJobPct(Math.round(100 * s.progress / (s.total || 1))))
      if (st.status === 'done' && st.result && pidRef.current === pid) {
        mutate((p) => {
          const c = p.clips.find((x) => x.id === clipId)
          if (c?.bg) {
            c.bg.processed = true
            c.bg._cache = { path: st.result, in: jobIn, out: jobOut }
            c.bg.stale = c.in !== jobIn || c.out !== jobOut
          }
        }, false)
      }
    } finally {
      setBgJob(null)
    }
  }, [flushSave, mutate])

  // Background removal for EVERY clip (REN-163). One clip at a time on purpose:
  // segbg is a segmentation model per frame, and starting eleven of them at once
  // would take the machine down. Progress says which clip it is on and the
  // button becomes Stop — a silent multi-minute freeze on a long video would be
  // worse than the one-clip-at-a-time behaviour it replaces.
  const bgAllStop = useRef(false)
  const processBgAll = useCallback(async () => {
    const pid = pidRef.current
    await flushSave()
    const ids = (projRef.current?.clips || []).filter((c) => c.bg).map((c) => c.id)
    if (!ids.length) { showToast('No clip has background removal turned on'); return }
    bgAllStop.current = false
    let done = 0
    for (const clipId of ids) {
      if (bgAllStop.current || pidRef.current !== pid) break
      const at = projRef.current?.clips.find((x) => x.id === clipId)
      if (!at?.bg) continue
      const jobIn = at.in, jobOut = at.out
      setBgJob(clipId)
      setBgJobAll({ done, total: ids.length })
      setBgJobPct(0)
      try {
        const { job } = await api.segbg(pid, clipId)
        const st = await pollJob(job, (j) => setBgJobPct(Math.round(100 * j.progress / (j.total || 1))))
        if (st.status === 'done' && st.result && pidRef.current === pid) {
          mutate((p) => {
            const c = p.clips.find((x) => x.id === clipId)
            if (c?.bg) {
              c.bg.processed = true
              c.bg._cache = { path: st.result, in: jobIn, out: jobOut }
              c.bg.stale = c.in !== jobIn || c.out !== jobOut
            }
          }, false)
        }
      } catch { /* one clip failing must not abandon the rest */ }
      done++
    }
    setBgJob(null)
    setBgJobAll(null)
    if (bgAllStop.current) showToast(`Stopped — ${done} of ${ids.length} clips processed`)
  }, [flushSave, mutate, showToast])
  const stopBgAll = useCallback(() => { bgAllStop.current = true }, [])

  // ---------- face retouch preview (REN-84) ----------
  // Face track for a source, generated on demand, kept in runtime state (never
  // saved to project.json — it would bloat every autosave). Powers the honest
  // "face detected" chip and the live face-masked preview mask.
  const faceTrackKeys = useRef(new Set())
  // Tracks are also kept in a ref so a caller that has just awaited detectFace
  // can read them (auto zoom, REN-159) — React state is a render behind.
  const faceTracksRef = useRef({})
  const keepTrack = useCallback((srcKey, track) => {
    faceTracksRef.current = { ...faceTracksRef.current, [srcKey]: track }
    setFaceTracks((m) => ({ ...m, [srcKey]: track }))
  }, [])
  const detectFace = useCallback((srcKey) => {
    if (!srcKey) return Promise.resolve(null)
    const src = projRef.current?.sources?.[srcKey]
    const path = src?.proxy || src?.path // proxy = fast keyframe seeks
    if (!path) return Promise.resolve(null)
    if (faceTracksRef.current[srcKey]) return Promise.resolve(faceTracksRef.current[srcKey])
    if (faceTrackKeys.current.has(srcKey)) return Promise.resolve(null)
    faceTrackKeys.current.add(srcKey)
    const pid = pidRef.current
    return api.facedetect(pid, path).then((r) => {
      if (r.track) { keepTrack(srcKey, r.track); return r.track }
      return pollJob(r.job).then((st) => {
        if (st.status !== 'done' || pidRef.current !== pid) return null
        return api.facetrack(pid, r.path).then((track) => { keepTrack(srcKey, track); return track })
      })
    }).catch(() => { faceTrackKeys.current.delete(srcKey); return null })
  }, [keepTrack])

  // ── one button that gives the cut its rhythm (REN-159) ────────────────────
  //
  // What actually works in vertical social video, and why these numbers:
  //
  //  * ALTERNATE, never accumulate. Take 1 sits wide, take 2 comes in a little,
  //    take 3 goes back out. Growing take after take ends up cropped into the
  //    speaker's nostrils by take 6 — the mistake that makes an auto-zoom look
  //    automatic. The pattern below cycles 1.00 / 1.07 / 1.00 / 1.05, so the
  //    change is always relative to a wide neighbour and never compounds.
  //  * SMALL. Between 5% and 8% reads as a change of energy; past ~15% on a
  //    talking head it reads as a mistake, and on a 720p proxy it also reads as
  //    softness, because a punch-in is a crop.
  //  * HOLD, don't drift. Each take gets ONE scale for its whole length (two
  //    identical keyframes), not a slow push. A continuous move on every take
  //    is seasick; the cut itself is what carries the rhythm.
  //  * SKIP SHORT TAKES. Under ~1.2s there is no time to register a change,
  //    only a jolt.
  //  * FOLLOW THE FACE. The centre comes from the detected face, not the middle
  //    of the frame, or a punch-in on someone standing off-centre crops them
  //    out. Clamped so the face stays inside Instagram's safe area.
  //
  // Marked with `auto: true` so a second press removes exactly what it added
  // and leaves hand-made keyframes alone.
  const ZOOM_CYCLE = [1.0, 1.07, 1.0, 1.05]
  const autoZoom = useCallback(async () => {
    const p0 = projRef.current
    if (!p0?.clips?.length) return
    if (typeLocked(p0, 'clip')) { showToast('Video track is locked'); return }

    const hasAuto = p0.clips.some((c) => (c.kfs || []).some((k) => k.auto))
    if (hasAuto) {                                  // second press = take it off
      mutate((p) => {
        for (const c of p.clips) {
          if ((c.kfs || []).some((k) => k.auto)) c.kfs = (c.kfs || []).filter((k) => !k.auto)
        }
      })
      showToast('Auto zoom removed — your own keyframes are untouched')
      return
    }

    // face centres, per source, so the punch-in lands on him
    const srcs = [...new Set(p0.clips.map((c) => c.src || 'main'))]
    for (const k of srcs) { try { await detectFace(k) } catch { /* no track, use centre */ } }
    const tracks = faceTracksRef.current

    let skipped = 0, applied = 0
    mutate((p) => {
      let n = 0
      for (const c of p.clips) {
        const manual = (c.kfs || []).filter((k) => !k.auto)
        if (manual.length) { skipped++; continue }   // never fight his own zoom
        const d = c.out - c.in
        if (d < 1.2) { skipped++; continue }
        const scale = ZOOM_CYCLE[n % ZOOM_CYCLE.length]
        n++
        if (scale === 1) { c.kfs = manual; continue }  // the wide beats stay wide
        // where he is, averaged over this clip (facedetect writes x/y as the
        // face CENTRE, 0..1 of the frame)
        const ss = (tracks[c.src || 'main']?.samples || [])
          .filter((x) => x.t >= c.in && x.t <= c.out && x.x != null)
        let cx = 50, cy = 45
        if (ss.length) {
          cx = 100 * ss.reduce((a, x) => a + x.x, 0) / ss.length
          cy = 100 * ss.reduce((a, x) => a + x.y, 0) / ss.length
        }
        // A punch-in of `scale` crops to 1/scale of the frame, so a centre this
        // far from the middle is as far as it can go before the crop runs off
        // the edge. Clamped again to the safe area so his face never lands
        // where Instagram puts its own buttons.
        const room = 50 * (1 - 1 / scale)
        cx = Math.max(50 - room, Math.min(50 + room, cx))
        cy = Math.max(Math.max(50 - room, 20), Math.min(Math.min(50 + room, 78), cy))
        c.kfs = [...manual,
                 { t: 0, scale, cx: +cx.toFixed(1), cy: +cy.toFixed(1), auto: true },
                 { t: +d.toFixed(2), scale, cx: +cx.toFixed(1), cy: +cy.toFixed(1), auto: true }]
        applied++
      }
    })
    showToast(applied
      ? `Zoom on ${applied} take${applied > 1 ? 's' : ''}${skipped ? `, ${skipped} left alone` : ''} — press again to remove`
      : 'Nothing to zoom — the takes are too short, or they already have keyframes')
  }, [mutate, showToast, detectFace])

  // Accurate retouch preview: render this clip through the real pipeline at
  // proxy res (mirrors processBg). Stores rt._cache + processed/stale on the clip.
  const processRt = useCallback(async (clipId) => {
    const pid = pidRef.current
    await flushSave()
    const at = projRef.current?.clips.find((x) => x.id === clipId)
    if (!at?.rt) return
    const jobIn = at.in, jobOut = at.out, sig = rtSig(at.rt)
    setRtJob(clipId)
    setRtJobPct(0)
    try {
      const { job } = await api.retouch(pid, clipId)
      const st = await pollJob(job, (s) => setRtJobPct(Math.round(100 * s.progress / (s.total || 1))))
      if (st.status === 'done' && st.result && pidRef.current === pid) {
        mutate((p) => {
          const c = p.clips.find((x) => x.id === clipId)
          if (c?.rt) {
            c.rt.processed = true
            c.rt._cache = { path: st.result, in: jobIn, out: jobOut, sig }
            c.rt.stale = c.in !== jobIn || c.out !== jobOut
          }
        }, false)
      }
    } finally {
      setRtJob(null)
    }
  }, [flushSave, mutate])

  // ---------- transcription editing (Descript-style, REN-83) ----------
  // Per-source transcripts live on disk (sources[key].transcript); their content
  // is fetched into runtime state. A "deleted" word is DERIVED (its source span
  // isn't covered by any clip) — never stored, so it can't desync.
  const transcriptKeys = useRef(new Set())
  const loadTranscripts = useCallback(() => {
    const p = projRef.current
    if (!p) return
    const pid = pidRef.current
    for (const [key, src] of Object.entries(p.sources || {})) {
      if (!src.transcript || transcripts[key] || transcriptKeys.current.has(`load:${key}`)) continue
      transcriptKeys.current.add(`load:${key}`)
      api.transcript(pid, src.transcript)
        .then((t) => setTranscripts((m) => ({ ...m, [key]: t })))
        .catch(() => transcriptKeys.current.delete(`load:${key}`))
    }
  }, [transcripts])

  // proj/id can be passed explicitly right after an import — the refs only
  // update on the next render, so a fresh import would otherwise be a no-op.
  const transcribeSources = useCallback((lang = 'auto', proj, id) => {
    const p = proj || projRef.current
    if (!p) return
    const pid = id || pidRef.current
    for (const [key, src] of Object.entries(p.sources || {})) {
      if (transcripts[key] || src.transcript || !src.path || transcriptKeys.current.has(`job:${key}`)) continue
      transcriptKeys.current.add(`job:${key}`)
      setTranscribing((t) => [...t, key])
      api.transcribeSource(pid, key, src.path, lang).then(({ job, path }) =>
        pollJob(job).then((st) => {
          transcriptKeys.current.delete(`job:${key}`)
          setTranscribing((t) => t.filter((k) => k !== key))
          if (st.status !== 'done' || pidRef.current !== pid) return
          mutate((pp) => { if (pp.sources?.[key]) pp.sources[key].transcript = path }, false)
          api.transcript(pid, path).then((t) => setTranscripts((m) => ({ ...m, [key]: t })))
        })
      ).catch(() => {
        transcriptKeys.current.delete(`job:${key}`)
        setTranscribing((t) => t.filter((k) => k !== key))
        showToast('Transcription failed')
      })
    }
  }, [transcripts, mutate, showToast])
  transcribeRef.current = transcribeSources

  // Cut one or more source spans out of the timeline in a SINGLE undo entry
  // (REN-113 range delete, possibly spanning several sources/takes): trims edges,
  // splits interiors, absorbs fragments <0.15s. Captions are source-anchored so
  // they follow automatically. Accepts an array [{src, s0, s1}] (or a single
  // span for back-compat).
  const transcriptDelete = useCallback((spans) => {
    if (typeLocked(projRef.current, 'clip')) { showToast('Video track is locked'); return }
    const list = Array.isArray(spans) ? spans : [spans]
    if (!list.length) return
    mutateClips((p) => {
      const ABSORB = 0.15
      for (const { src, s0, s1 } of list) {
        const out = []
        for (const c of p.clips) {
          if ((c.src || 'main') !== src) { out.push(c); continue }
          const a = Math.max(s0, c.in), b = Math.min(s1, c.out)
          if (b <= a) { out.push(c); continue }         // span doesn't touch this clip
          if (a - c.in >= ABSORB) {                       // keep the head
            const L = clone(c); L.out = +a.toFixed(3)
            if (L.bg?.processed) L.bg.stale = true
            if (L.rt?.processed) L.rt.stale = true
            out.push(L)
          }
          if (c.out - b >= ABSORB) {                      // keep the tail (new clip)
            const R = clone(c); R.id = uid('c'); R.in = +b.toFixed(3); R.kfs = []; R.fadeIn = 0
            if (R.bg) { R.bg.stale = !!R.bg.processed; delete R.bg._cache }
            if (R.rt) { R.rt.stale = !!R.rt.processed; delete R.rt._cache }
            out.push(R)
          }
        }
        p.clips = out
      }
    })
  }, [mutateClips, showToast])

  // Restore a trimmed-out word: extend the nearest same-source clip edge to cover
  // it, then merge clips that became contiguous (heals a split).
  const transcriptRestore = useCallback((src, w) => {
    if (typeLocked(projRef.current, 'clip')) { showToast('Video track is locked'); return }
    mutateClips((p) => {
      const LEAD = 0.03, TAIL = 0.05
      const srcDur = p.sources?.[src]?.duration ?? (w.t1 + TAIL)
      const want0 = Math.max(0, w.t0 - LEAD)
      const want1 = Math.min(srcDur, w.t1 + TAIL)
      const idxs = p.clips.map((c, i) => ({ c, i })).filter((x) => (x.c.src || 'main') === src)
      if (!idxs.length) return
      let target = null, mode = null, best = Infinity
      for (const { c, i } of idxs) {
        if (c.out <= want0 + 1e-3) { const g = want0 - c.out; if (g < best) { best = g; target = c; mode = 'out' } }
        else if (c.in >= want1 - 1e-3) { const g = c.in - want1; if (g < best) { best = g; target = c; mode = 'in' } }
      }
      if (!target) { // fall back to the clip whose edge is nearest the word center
        const mid = (w.t0 + w.t1) / 2
        for (const { c } of idxs) {
          const d = Math.min(Math.abs(mid - c.in), Math.abs(mid - c.out))
          if (d < best) { best = d; target = c; mode = mid < c.in ? 'in' : 'out' }
        }
      }
      if (!target) return
      if (mode === 'out') target.out = +want1.toFixed(3)
      else target.in = +want0.toFixed(3)
      if (target.bg?.processed) target.bg.stale = true
      if (target.rt?.processed) target.rt.stale = true
      for (let i = p.clips.length - 2; i >= 0; i--) {
        const a = p.clips[i], b = p.clips[i + 1]
        if ((a.src || 'main') === src && (b.src || 'main') === src && b.in <= a.out + 1e-2 && b.in >= a.in) {
          a.out = +Math.max(a.out, b.out).toFixed(3)
          p.clips.splice(i + 1, 1)
        }
      }
    })
  }, [mutateClips, showToast])

  // Split the source's covering clip at source-time atT (separate takes).
  const transcriptSplitSource = useCallback((src, atT) => {
    if (typeLocked(projRef.current, 'clip')) { showToast('Video track is locked'); return }
    mutateClips((p) => {
      const i = p.clips.findIndex((c) => (c.src || 'main') === src && atT > c.in + 0.1 && atT < c.out - 0.1)
      if (i < 0) return
      const c = p.clips[i]
      const a = clone(c); a.out = +atT.toFixed(3); a.fadeOut = 0
      const b = clone(c); b.in = +atT.toFixed(3); b.id = uid('c'); b.fadeIn = 0; b.kfs = []
      if (a.bg?.processed) a.bg.stale = true
      if (b.bg) { b.bg.stale = !!b.bg.processed; delete b.bg._cache }
      if (a.rt?.processed) a.rt.stale = true
      if (b.rt) { b.rt.stale = !!b.rt.processed; delete b.rt._cache }
      p.clips.splice(i, 1, a, b)
    })
  }, [mutateClips, showToast])

  // ---------- export (runs in background; modal can close and reopen) ----------
  const startExport = useCallback(async (quality) => {
    const pid = pidRef.current
    await flushSave()
    setExp({ phase: 'rendering', pct: 0, quality, out: null, filename: null, log: [] })
    try {
      const { job, out, filename } = await api.export(pid, quality)
      const st = await pollJob(job, (s) =>
        setExp((e) => ({ ...e, phase: 'rendering', pct: Math.round(100 * s.progress / (s.total || 1)), out, filename })))
      if (st.status === 'done') setExp((e) => ({ ...e, phase: 'done', pct: 100, out, filename }))
      else setExp((e) => ({ ...e, phase: 'error', log: st.log || [] }))
    } catch (err) {
      setExp((e) => ({ ...e, phase: 'error', log: [String(err)] }))
    }
  }, [flushSave])

  // Reopening shows a live render; a finished done/error state resets to the
  // option cards so the project can always be exported again
  const openExport = useCallback(() => {
    setExp((e) => (e.phase === 'rendering' ? e : { ...e, phase: 'choose', pct: 0 }))
    setModal('export')
  }, [])

  // ---------- auto captions (transcription job) ----------
  const openTranscribe = useCallback(() => {
    setTransc((t) => (t.phase === 'running' ? t : { phase: 'choose', pct: 0, added: null, log: [] }))
    setModal('transcribe')
  }, [])

  const startTranscribe = useCallback(async (lang) => {
    const pid = pidRef.current
    await flushSave()
    setTransc({ phase: 'running', pct: 0, added: null, log: [] })
    try {
      const { job } = await api.transcribe(pid, lang)
      const st = await pollJob(job, (s) =>
        setTransc((t) => ({ ...t, pct: Math.round(100 * s.progress / (s.total || 1)) })))
      if (pidRef.current !== pid) return
      if (st.status === 'done') {
        const p = await api.get(pid)
        if (pidRef.current !== pid) return
        mtimeRef.current = p._mtime
        setProject(p)
        setSaveState('saved')
        refreshHistory()
        // close the modal right away so the fresh captions are visible on the
        // timeline + preview immediately (no extra "done" screen to dismiss)
        setModal(null)
        setTransc({ phase: 'choose', pct: 0, added: null, log: [] })
      } else {
        setTransc({ phase: 'error', pct: 0, added: null, log: st.log || [] })
      }
    } catch (err) {
      setTransc({ phase: 'error', pct: 0, added: null, log: [String(err)] })
    }
  }, [flushSave, refreshHistory])

  // ---------- chat ----------
  // textOverride lets a preset run its own prompt (Chat's onClick passes an
  // event, so only a real string counts as an override). presetId tells the
  // server which preset ran, so it can enforce the approved-script gate.
  const sendChat = useCallback(async (textOverride, presetId) => {
    const txt = (typeof textOverride === 'string' ? textOverride : chatInput).trim()
    const pid = pidRef.current
    if (!txt || claudeTyping) return
    // flush the debounced autosave FIRST: approving the script and clicking the
    // preset happen back-to-back, and the server reads project.json at request
    // time — an unflushed scriptApproved would re-run the script step and
    // regenerate over the user's corrected script (REN-128 review).
    await flushSave()
    setChatMsgs((m) => [...m, { who: 'you', text: txt, ts: Date.now() / 1000 }])
    if (typeof textOverride !== 'string') setChatInput('') // a preset run keeps your typed text
    typingTimer.current = setTimeout(() => setClaudeTyping(true), 500)
    try {
      const { job, busy } = await api.chatSend(pid, txt, chatModel, chatEffort,
        typeof presetId === 'string' ? presetId : undefined)
      if (busy) {
        // a previous edit is still running — re-attach to it instead of erroring;
        // drop the optimistic bubble and put the new text back in the composer
        setChatMsgs((m) => m.slice(0, -1))
        setChatInput(txt)
      }
      await pollJob(job)
      if (pidRef.current !== pid) return // switched projects while Claude worked
      const [msgs, p] = await Promise.all([api.chat(pid), api.get(pid)])
      if (pidRef.current !== pid) return
      setChatMsgs(msgs)
      mtimeRef.current = p._mtime
      setProject(p)
      setSaveState('saved')
      refreshHistory()
    } catch (err) {
      if (pidRef.current === pid) {
        setChatMsgs((m) => [...m, { who: 'claude', text: err?.gone
          ? '⚠ The server restarted while this was running, so it lost track of the job. Nothing was damaged — send the request again.'
          : `⚠ Chat failed: ${err}`, ts: Date.now() / 1000 }])
      }
    } finally {
      clearTimeout(typingTimer.current)
      setClaudeTyping(false)
    }
  }, [chatInput, claudeTyping, chatModel, chatEffort, flushSave, refreshHistory])

  // approve the script SERVER-SIDE (no debounce race) — if an edit request was
  // parked waiting for approval, the server resumes it and we attach to the job
  // exactly like a chat send (REN-129 single-flow).
  const approveScript = useCallback(async () => {
    const pid = pidRef.current
    await flushSave()
    let r = await api.approveScript(pid)
    mutate((p) => { p.scriptApproved = true }, false)
    // attach to whatever is running; when it was busy (a script-step still
    // finishing) re-check afterwards — the server's finally-block resumes the
    // parked edit, and we attach to THAT job too, until nothing is left.
    let rounds = 0
    while (r.job && pidRef.current === pid && rounds < 4) {
      rounds++
      setLeftTab('claude')
      try { const msgs = await api.chat(pid); if (pidRef.current === pid) setChatMsgs(msgs) } catch { /* noop */ }
      typingTimer.current = setTimeout(() => setClaudeTyping(true), 300)
      try {
        await pollJob(r.job)
        if (pidRef.current !== pid) return
        const [msgs, p] = await Promise.all([api.chat(pid), api.get(pid)])
        if (pidRef.current !== pid) return
        setChatMsgs(msgs)
        mtimeRef.current = p._mtime
        setProject(p)
        setSaveState('saved')
        refreshHistory()
      } catch (err) {
        if (pidRef.current === pid) {
          setChatMsgs((m) => [...m, { who: 'claude', text: err?.gone
          ? '⚠ The server restarted while this was running, so it lost track of the job. Nothing was damaged — send the request again.'
          : `⚠ Chat failed: ${err}`, ts: Date.now() / 1000 }])
        }
        return
      } finally {
        clearTimeout(typingTimer.current)
        setClaudeTyping(false)
      }
      if (!r.busy) break
      r = await api.approveScript(pid)
    }
  }, [flushSave, mutate, refreshHistory])

  // ---------- audio output device (REN-140) ---------------------------------
  // Which physical output the preview plays through. Lives here, not in the
  // player, so the control can sit somewhere unobtrusive in the header.
  // Chrome hides device NAMES until the page holds a media permission, so the
  // list stays empty until he asks for it — that request is explicit, on click,
  // and the microphone track is stopped the same instant it is granted.
  const sinkSupported = typeof HTMLMediaElement !== 'undefined'
    && 'setSinkId' in HTMLMediaElement.prototype
  const [sinks, setSinks] = useState([])
  const [sinkId, setSinkIdState] = useState(() => localStorage.getItem('vs-sink') || '')
  const setSinkId = useCallback((id) => {
    setSinkIdState(id)
    localStorage.setItem('vs-sink', id)
  }, [])
  const loadSinks = useCallback(async () => {
    if (!sinkSupported) return []
    try {
      let devs = await navigator.mediaDevices.enumerateDevices()
      if (!devs.some((d) => d.kind === 'audiooutput' && d.label)) {
        const st = await navigator.mediaDevices.getUserMedia({ audio: true })
        st.getTracks().forEach((t) => t.stop())   // permission only — nothing is recorded
        devs = await navigator.mediaDevices.enumerateDevices()
      }
      const outs = devs.filter((d) => d.kind === 'audiooutput' && d.deviceId && d.label)
      setSinks(outs)
      return outs
    } catch { return [] }
  }, [sinkSupported])
  useEffect(() => { if (sinkSupported && sinkId) loadSinks() }, [])

  // ---------- script ready alert (REN-141) ----------------------------------
  // When a preset writes the script, nothing told him it was ready — he had to
  // go look at the Script tab. This fires ONLY for a script that arrived from
  // the server (preset step or "Gerar do vídeo"); his own typing must never
  // re-trigger it, which is why it is not a plain effect on project.script.
  const seenScriptRef = useRef(null)
  const [scriptAlert, setScriptAlert] = useState(null)

  const notifyReady = useCallback((title, body) => {
    try {
      // Only badge/flag when he CAN'T see it. Badging a focused window leaves a
      // dot that never clears: the focus listener that clears it won't fire
      // because the window was already focused.
      if (document.hasFocus?.()) return   // he is already looking — modal is enough
      navigator.setAppBadge?.(1)
      document.title = `● ${title} — AI Video Studio`
      if (!('Notification' in window)) return
      const show = () => new Notification(title, { body, tag: 'vs-script' })
      if (Notification.permission === 'granted') show()
      else if (Notification.permission !== 'denied') {
        Notification.requestPermission().then((pm) => pm === 'granted' && show())
      }
    } catch { /* notifications unavailable — the modal still shows */ }
  }, [])

  const noteServerScript = useCallback((p) => {
    const s = (p?.script || '').trim()
    if (!s || p?.scriptApproved) { seenScriptRef.current = s; return }
    if (s === seenScriptRef.current) return
    seenScriptRef.current = s
    setScriptAlert(s)
    setLeftTab('script')
    notifyReady('Script ready', 'Review and approve it so the AI can carry on with the edit.')
  }, [notifyReady])

  useEffect(() => {
    const clear = () => {
      navigator.clearAppBadge?.()
      if (document.title.startsWith('●')) document.title = 'AI Video Studio'
    }
    window.addEventListener('focus', clear)
    return () => window.removeEventListener('focus', clear)
  }, [])

  // Whatever script is on screen counts as SEEN — however it got there. Without
  // this the ref only advanced when the alert fired, so after he edited the
  // script himself the next server write (any chat job) reloaded HIS text and
  // popped "Roteiro pronto" over his own words. noteServerScript still wins the
  // race on a real arrival: the poll calls it synchronously right after
  // setProject, before this effect runs.
  useEffect(() => {
    const t = (project?.script || '').trim()
    if (t) seenScriptRef.current = t
  }, [project?.script])

  // ---------- script (REN-123): AI deduces the clean intended script ----------
  const generateScript = useCallback(async () => {
    const pid = pidRef.current
    await flushSave() // don't let a pending autosave race the server write
    const { job } = await api.generateScript(pid, chatModel)
    const st = await pollJob(job)
    if (pidRef.current !== pid) return
    if (st.status === 'error') throw new Error((st.log && st.log[0]) || 'script generation failed')
    // merge ONLY the fields the job changed into the live project — never replace
    // the whole project (would drop concurrent timeline edits), and the next
    // autosave re-persists the script so a stale client PUT can't clobber it.
    // A regenerated script is UNREVIEWED — approval is revoked.
    mutate((p) => { p.script = st.result; p.scriptApproved = false }, false)
    noteServerScript({ script: st.result, scriptApproved: false })
  }, [chatModel, flushSave, mutate, noteServerScript])

  // apply the (edited) script's wording/spelling/numbers to the captions, keeping
  // the timing — an AI edit of project.json, so reload like a chat edit.
  const applyScriptToCaptions = useCallback(async () => {
    const pid = pidRef.current
    await flushSave()
    const { job } = await api.applyScriptToCaptions(pid, chatModel)
    const st = await pollJob(job)
    if (pidRef.current !== pid) return
    if (st.status === 'error') throw new Error((st.log && st.log[0]) || 'failed')
    const proj = await api.get(pid)
    if (pidRef.current !== pid) return
    mtimeRef.current = proj._mtime
    // merge ONLY the captions (what the job changed) — never replace the whole
    // project, so a concurrent timeline edit during the run isn't dropped.
    mutate((p) => { p.captions = proj.captions }, false)
    refreshHistory()
  }, [chatModel, flushSave, mutate, refreshHistory])

  // ---------- history ----------
  const restoreSnapshot = useCallback(async (file) => {
    const pid = pidRef.current
    if (saveTimer.current) await flushSave() // the pre-restore snapshot must hold the latest edits
    const p = await api.restore(pid, file)
    pastRef.current = []
    futureRef.current = []
    pendingGestureRef.current = null
    mtimeRef.current = p._mtime
    setProject(p)
    setSel(null)
    setModal(null)
    setSaveState('saved')
    setHistStamp((s) => s + 1)
    refreshHistory()
  }, [refreshHistory, flushSave])

  // ---------- library actions ----------
  const importNewProject = useCallback(async (file) => {
    setImporting(true)
    try {
      const r = await api.importProject(file)
      await openProject(r.id, r.project)
      // start transcribing right away, in the background (REN-133): whisper is
      // the slow step, and doing it at import means it's already done by the
      // time you ask for an edit — no waiting inside the AI job.
      transcribeSources('auto', r.project, r.id)
      // when the 540p proxy lands, adopt it in the working copy too (otherwise
      // a later autosave would clobber the server-side attach with proxy: null)
      if (r.proxyJob) {
        pollJob(r.proxyJob).then((st) => {
          if (st.status !== 'done' || pidRef.current !== r.id) return
          mutate((p) => {
            if (p.sources?.main) p.sources.main.proxy = 'media/proxy_main.mp4'
          }, false)
        })
      }
    } finally {
      setImporting(false)
    }
  }, [openProject, mutate, transcribeSources])

  const startEmpty = useCallback(async () => {
    const r = await api.create('Untitled project')
    await openProject(r.id, r.project)
  }, [openProject])

  const duplicateProject = useCallback(async (pid) => {
    await api.duplicate(pid)
    loadLibrary()
  }, [loadLibrary])

  const removeProject = useCallback(async (pid, name) => {
    if (!window.confirm(`Delete “${name}”? This cannot be undone.`)) return
    await api.remove(pid)
    loadLibrary()
  }, [loadLibrary])

  // Route a set of dropped/pasted files by type + target region (REN-85).
  // ctx: { target:'library'|'player'|'timeline', x?, y?, t0?, insertIndex?, asTake? }
  const dropMedia = useCallback(async (files, ctx) => {
    const list = Array.from(files || [])
    if (!list.length) return
    if (ctx.target === 'library') {
      const vid = list.find((f) => classifyFile(f) === 'video')
      if (vid) return importNewProject(vid)
      showToast('Drop a video to start a new project')
      return
    }
    const supported = list.filter((f) => classifyFile(f))
    if (!supported.length) { showToast('Unsupported file type'); return }
    if (supported.length < list.length) showToast('Some files were an unsupported type')
    beginGesture() // the whole drop is one undo entry
    let imgN = 0, vidN = 0
    const p0 = projRef.current
    for (const file of supported) {
      const kind = classifyFile(file)
      // refuse a drop onto a locked track (REN-112)
      const trackKey = kind === 'audio' ? 'audio' : (kind === 'video' && ctx.asTake) ? 'video' : 'overlays'
      if (p0?.tracks?.[trackKey]?.locked) { showToast(`${trackKey} track is locked`); continue }
      try {
        if (kind === 'video' && ctx.asTake) {
          // offset per video so a multi-select lands in drop order, not reversed
          await addTakeAt(file, ctx.insertIndex == null ? undefined : ctx.insertIndex + vidN++)
        } else if (kind === 'audio') await addAudioAt(file, ctx.t0)
        else {
          const off = kind === 'image' ? (imgN++ * 2) : 0 // stack multiple images
          await addOverlayAt(file, kind === 'image' ? 'image' : 'video',
            { x: (ctx.x ?? 50) + off, y: (ctx.y ?? 40) + off, t0: ctx.t0 })
        }
      } catch { showToast(`Couldn’t add ${file.name}`) }
    }
  }, [importNewProject, beginGesture, addTakeAt, addAudioAt, addOverlayAt, showToast])

  // Cmd+V: paste an image from the clipboard as an overlay at the playhead
  const pasteImage = useCallback((file) => {
    if (screenRef.current !== 'editor' || !pidRef.current) return
    dropMedia([file], { target: 'player', x: 50, y: 40, t0: +timeStore.get().toFixed(1) })
  }, [dropMedia])

  return {
    screen, library, currentId, project, playing, setPlaying,
    setTime: timeStore.set, getTime: timeStore.get,
    pxPerSec, setPxPerSec, sel, setSel, saveState, modal, setModal,
    fullscreen, setFullscreen, importing, vw, isMobile: vw < 760,
    menuOpen, setMenuOpen, compare, setCompare, inlineEdit, setInlineEdit,
    exp, transc, bgJob, bgJobPct, bgJobAll, processBgAll, stopBgAll,
    rtJob, rtJobPct, faceTracks, detectFace, processRt,
    capApplyAll, setCapApplyAll, spreadCapLook, splitCaption, moveCaption, autoZoom,
    toast, busyMedia, showToast, dropMedia, pasteImage, classifyFile, appVersion,
    leftTab, setLeftTab, transcripts, transcribing, loadTranscripts, transcribeSources,
    transcriptDelete, transcriptRestore, transcriptSplitSource,
    chatOpen, setChatOpen, chatMsgs, chatInput, setChatInput, claudeTyping,
    chatModel, chatEffort, setChatModel, setChatEffort, engines, models, rescanModels,
    presets, savePresets, editingPreset, setEditingPreset, generateScript, applyScriptToCaptions,
    scriptAlert, setScriptAlert,
    sinkSupported, sinks, sinkId, setSinkId, loadSinks,
    approveScript,
    snapshots, histStamp,
    openProject, backToProjects, loadLibrary,
    mutate, beginGesture, undo, redo, canUndo, canRedo,
    deleteSel, addCaption, addText, addOverlayFile, addAudioFile, addTakeFile,
    setAspect, splitClip, processBg, rtSig, toggleTrack, renameTrack,
    startExport, openExport, openTranscribe, startTranscribe, sendChat, restoreSnapshot,
    importNewProject, startEmpty, duplicateProject, removeProject,
  }
}
