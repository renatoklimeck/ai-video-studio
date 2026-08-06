import { useEffect, useRef, useState } from 'react'
import LeftPanel from './LeftPanel'
import Player from './Player'
import Inspector from './Inspector'
import Timeline from './Timeline'
import AudioOut from './AudioOut'
import UpdateButton from './UpdateButton'
import { ExportModal, HistoryModal, TranscribeModal, PreferencesModal, PresetModal, ScriptAlertModal } from './Modals'

export default function Editor({ s }) {
  const takeFileRef = useRef(null)
  const ovFileRef = useRef(null)
  const audFileRef = useRef(null)
  const [drag, setDrag] = useState(null) // external file drag-over affordances (REN-85)
  const p = s.project

  // Cmd+V pastes a clipboard image as an overlay at the playhead
  useEffect(() => {
    const onPaste = (e) => {
      const item = [...(e.clipboardData?.items || [])].find((it) => it.type.startsWith('image/'))
      const f = item?.getAsFile()
      if (f) s.pasteImage(f)
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [s.pasteImage])

  if (!p) return null

  // the button is a toggle, so it has to know whether its own keyframes are on
  const autoZoomOn = (p.clips || []).some((c) => (c.kfs || []).some((k) => k.auto))

  const isFileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
  // routes a drop by the region under the cursor + returns rects for affordances
  const analyzeDrag = (e) => {
    const x = e.clientX, y = e.clientY
    const q = (sel) => document.querySelector(sel)?.getBoundingClientRect()
    const frame = q('.preview-frame'), vlane = q('.tl-videolane'), tl = q('.timeline'), ruler = q('.tl-ruler')
    const inR = (r) => r && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom
    const timeAt = () => (ruler ? Math.max(0, (x - ruler.left) / s.pxPerSec) : 0)
    if (inR(frame)) return { region: 'player', frame, x: ((x - frame.left) / frame.width) * 100,
      y: ((y - frame.top) / frame.height) * 100, t0: s.getTime(), asTake: e.altKey, alt: e.altKey }
    if (inR(vlane)) {
      const t = timeAt(); let acc = 0; const bounds = []
      for (let i = 0; i <= p.clips.length; i++) { bounds.push(acc); if (i < p.clips.length) acc += p.clips[i].out - p.clips[i].in }
      let bi = 0, best = 1e9
      bounds.forEach((bt, i) => { const d = Math.abs(bt - t); if (d < best) { best = d; bi = i } })
      const caretX = Math.max(tl.left, Math.min(tl.right, (ruler ? ruler.left : vlane.left) + bounds[bi] * s.pxPerSec))
      return { region: 'videolane', vlane, tl, caretX, insertIndex: bi, t0: t, asTake: true }
    }
    if (inR(tl)) return { region: 'timeline', tl, t0: timeAt() }
    return { region: 'player', frame, x: 50, y: 40, t0: s.getTime() }
  }
  const onDragOver = (e) => {
    if (!isFileDrag(e)) return
    e.preventDefault()
    if (e.altKey && e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    setDrag(analyzeDrag(e))
  }
  const onDragLeave = (e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDrag(null) }
  const onDrop = (e) => {
    if (!isFileDrag(e)) return
    e.preventDefault()
    const info = analyzeDrag(e)
    setDrag(null)
    const files = e.dataTransfer.files
    if (info.region === 'player') s.dropMedia(files, { target: 'player', x: info.x, y: info.y, t0: info.t0, asTake: info.asTake })
    else if (info.region === 'videolane') s.dropMedia(files, { target: 'timeline', insertIndex: info.insertIndex, t0: info.t0, asTake: true })
    else s.dropMedia(files, { target: 'timeline', t0: info.t0, asTake: false })
  }

  const pickFile = (ref) => () => ref.current?.click()
  const onFile = (fn) => (e) => {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (f) fn(f)
  }

  const aspectSeg = (h) => (
    <div className="seg" style={h ? { height: h } : undefined}>
      <button className={p.aspect === '9:16' ? 'on' : ''} title="Vertical (Reels/TikTok)"
              style={h ? { height: h } : undefined}
              onClick={() => { s.setAspect('9:16'); s.setMenuOpen(false) }}>9:16</button>
      <button className={p.aspect === '16:9' ? 'on' : ''} title="Horizontal (YouTube)"
              style={h ? { height: h } : undefined}
              onClick={() => { s.setAspect('16:9'); s.setMenuOpen(false) }}>16:9</button>
    </div>
  )

  return (
    <div className="editor" onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop} onDragEnd={() => setDrag(null)}>
      {/* ── header ── */}
      <div className="header">
        <button className="btn-icon" title="Back to projects" onClick={s.backToProjects}>←</button>
        <div className="logo-tile sm"><div className="tri" /></div>
        {/* Directly above the panel it opens and closes. It used to be a wide
            green button reading "Claude", parked on the far RIGHT of the header
            — the opposite end of the screen from the thing it controls, and
            named after an engine it has nothing to do with (a Codex user was
            told "Claude"). Which engine runs the edit is shown where it is
            actually chosen: the panel's own tab and the Model picker. */}
        {!s.isMobile && (
          <button className={`btn-icon panel-toggle ${s.chatOpen ? 'on' : ''}`}
                  title={s.chatOpen ? 'Hide the AI panel' : 'Show the AI panel'}
                  onClick={() => s.setChatOpen(!s.chatOpen)}>◧</button>
        )}
        <div className="name-wrap">
          <div className="proj-title">{s.isMobile ? p.name : `${p.name} · ${p.w}×${p.h}`}</div>
          <div className={`save-ind ${s.saveState}`}>
            {s.saveState === 'saving' ? '● saving…' : '✓ all changes saved'}
          </div>
        </div>
        {!s.isMobile && (
          <>
            <div style={{ marginLeft: 4 }}>{aspectSeg()}</div>
            <div className="divider" />
            <div className="add-group">
              <button className="btn-add teal" title="Import source video as a new take"
                      onClick={pickFile(takeFileRef)}>+ Video</button>
              <button className="btn-add" onClick={s.addCaption}>+ Caption</button>
              <button className="btn-add" onClick={s.addText}>+ Text</button>
              <button className="btn-add" onClick={pickFile(ovFileRef)}>+ Media</button>
              <button className="btn-add" onClick={pickFile(audFileRef)}>+ Music</button>
              <button className="btn-add" title="Transcribe the voice into word-synced captions (re-run after cuts to refresh them)"
                      onClick={s.openTranscribe}>{s.project?.captions?.length ? 'Retranscribe' : 'Transcribe'}</button>
              {/* REN-159 — alternating punch-ins, centred on the face; press
                  again to take them off */}
              <button className={`btn-add ${autoZoomOn ? 'teal' : ''}`}
                      title={autoZoomOn
                        ? 'Remove the automatic zoom (your own keyframes stay)'
                        : 'Give the cut some rhythm: a small punch-in on alternate takes, centred on your face'}
                      onClick={s.autoZoom}>{autoZoomOn ? '✓ Auto zoom' : '⤢ Auto zoom'}</button>
            </div>
          </>
        )}
        <div className="spacer" />
        {!s.isMobile && (
          <>
            <button className="btn-icon" title="Undo (⌘Z)" disabled={!s.canUndo} onClick={s.undo}>↩</button>
            <button className="btn-icon" title="Redo (⇧⌘Z)" disabled={!s.canRedo} onClick={s.redo}>↪</button>
            <button className="btn-icon" title="Version history" onClick={() => s.setModal('history')}>⟲</button>
            <AudioOut s={s} />
            {/* which release this is (REN-168) — a student reporting a bug can
                read it off the header instead of digging in the terminal */}
            {s.appVersion && <span className="app-version" title="Version of AI Video Studio">{s.appVersion}</span>}
          </>
        )}
        {/* OUTSIDE the !isMobile block on purpose. It used to live inside, and
            since the editor is where you actually spend your time, a student
            who opened the app on a phone and went straight into a project was
            never told a new version existed — they could sit on a version with
            bugs already fixed for months. It renders nothing at all when there
            is nothing to install, so it costs no header space in the normal
            case. The version NUMBER stays desktop-only (it is reference text,
            not an action) and moves into the ☰ menu below on mobile. */}
        <UpdateButton />
        <button className="btn-primary" onClick={s.openExport}>Export</button>
        {s.isMobile && (
          <button className="btn-menu" title="Menu" onClick={() => s.setMenuOpen(!s.menuOpen)}>☰</button>
        )}
      </div>

      <input ref={takeFileRef} type="file" accept="video/*" hidden onChange={onFile(s.addTakeFile)} />
      <input ref={ovFileRef} type="file" accept="video/*,image/*" hidden onChange={onFile(s.addOverlayFile)} />
      <input ref={audFileRef} type="file" accept="audio/*" hidden onChange={onFile(s.addAudioFile)} />

      {/* ── mobile menu ── */}
      {s.menuOpen && (
        <div className="menu-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) s.setMenuOpen(false) }}>
          <div className="menu-card">
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.setLeftTab('claude'); s.setChatOpen(true) }}>
              <span className="dot" />{s.engineName} chat
            </button>
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.setLeftTab('script'); s.setChatOpen(true) }}>
              <span className="dot" />Script
            </button>
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.setLeftTab('transcript'); s.setChatOpen(true) }}>
              <span className="dot" />Cut (edit by text)
            </button>
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.undo() }}>↩ Undo</button>
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.redo() }}>↪ Redo</button>
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.setModal('history') }}>Version history</button>
            <button className="menu-row" onClick={() => { s.setMenuOpen(false); s.openTranscribe() }}>Transcribe captions</button>
            <div className="menu-div" />
            <div className="menu-aspect">
              <span>Aspect</span>
              {aspectSeg(32)}
            </div>
            {/* the release name, so someone reporting a bug from a phone can
                read it off instead of being asked to open a terminal */}
            {s.appVersion && (
              <div className="menu-version">
                <span className="app-version inline">{s.appVersion}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── middle row ── */}
      <div className="editor-middle">
        {s.chatOpen && <LeftPanel s={s} />}
        <Player s={s} onImportVideo={pickFile(takeFileRef)} />
        {(!s.isMobile || (s.sel && !s.inlineEdit)) && <Inspector s={s} />}
      </div>

      {/* ── mobile add bar ── */}
      {s.isMobile && (
        <div className="add-bar">
          <button className="teal" onClick={pickFile(takeFileRef)}>+ Video</button>
          <button onClick={s.addCaption}>+ Caption</button>
          <button onClick={s.addText}>+ Text</button>
          <button onClick={pickFile(ovFileRef)}>+ Media</button>
          <button onClick={pickFile(audFileRef)}>+ Music</button>
        </div>
      )}

      <Timeline s={s} />

      {s.modal === 'export' && <ExportModal s={s} />}
      {s.modal === 'history' && <HistoryModal s={s} />}
      {s.modal === 'transcribe' && <TranscribeModal s={s} />}
      {s.modal === 'prefs' && <PreferencesModal s={s} />}
      {s.modal === 'preset' && <PresetModal s={s} />}
      {s.scriptAlert && <ScriptAlertModal key={s.scriptAlert} s={s} />}

      {/* ── drag & drop affordances (fixed to client coords) ── */}
      {drag?.region === 'player' && drag.frame && (
        <div className="drop-frame" style={{ left: drag.frame.left, top: drag.frame.top,
          width: drag.frame.width, height: drag.frame.height }}>
          <div className="drop-ghost">{drag.alt ? 'Drop → new take' : 'Drop image / video / audio here'}</div>
        </div>
      )}
      {drag?.region === 'videolane' && drag.vlane && (
        <>
          <div className="drop-caret" style={{ left: drag.caretX, top: drag.vlane.top, height: drag.vlane.height }} />
          <div className="drop-lanehint" style={{ left: drag.tl.left, top: drag.vlane.top - 22 }}>insert take here · ⌥ from player</div>
        </>
      )}
      {drag?.region === 'timeline' && drag.tl && (
        <div className="drop-tl" style={{ left: drag.tl.left, top: drag.tl.top, width: drag.tl.width, height: drag.tl.height }} />
      )}

      {s.toast && <div className="vs-toast">{s.toast}</div>}
    </div>
  )
}
