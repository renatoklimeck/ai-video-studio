import { useState, useEffect } from 'react'
import { api } from './api'
import { fmtWhen } from './time'

const TRACK_NAMES = { video: 'Video', captions: 'Captions', texts: 'Texts', overlays: 'Overlays', audio: 'Audio' }

// Editor preferences (REN-120): durable style/rules the AI applies to every edit
// and grows automatically. Editable here so you always see (and can prune) what
// it "knows".
export function PreferencesModal({ s }) {
  const close = () => s.setModal(null)
  const [text, setText] = useState('')
  const [state, setState] = useState('loading') // loading | ready | saving | saved | error
  useEffect(() => {
    // On load failure DON'T present an empty, savable box — that would let a Save
    // overwrite (wipe) the real rules on disk. Show an error and lock saving.
    api.getPrefs().then((r) => { setText(r.text || ''); setState('ready') }).catch(() => setState('error'))
  }, [])
  const locked = state === 'loading' || state === 'saving' || state === 'error'
  const save = async () => {
    setState('saving')
    try { const r = await api.putPrefs(text); setText(r.text || ''); setState('saved'); setTimeout(() => setState('ready'), 1200) }
    catch { setState('ready') }
  }
  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="modal-card">
        <div className="modal-head">
          <div className="modal-title">Editor preferences</div>
          <span className="modal-x" onClick={close}>✕</span>
        </div>
        <div className="modal-body">
          <div className="modal-sub">
            Standing rules the AI follows on EVERY edit, on any video. It learns them from your
            feedback and adds them here — edit or delete anything you like. One rule per line.
          </div>
          <textarea
            className="prefs-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={'- when a line is repeated, keep the last complete take\n- cut only the silence between takes, never mid-sentence\n- captions in white Helvetica Bold, small, one line at a time'}
            disabled={locked}
          />
          <div className="modal-actions">
            <div className="chat-hint">
              {state === 'loading' ? 'loading…'
                : state === 'error' ? '⚠ could not load your preferences — close and reopen before saving'
                : state === 'saved' ? 'saved ✓'
                : 'the AI grows this list on its own'}
            </div>
            <button className="btn-modal primary" onClick={save} disabled={state !== 'ready' && state !== 'saved'}>
              {state === 'saving' ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// Create/edit a one-click chat preset (REN-122).
export function PresetModal({ s }) {
  const editing = s.editingPreset || {}
  const isNew = !editing.id
  const close = () => { s.setModal(null); s.setEditingPreset(null) }
  const [name, setName] = useState(editing.name || '')
  const [desc, setDesc] = useState(editing.desc || '')
  const [prompt, setPrompt] = useState(editing.prompt || '')
  const [busy, setBusy] = useState(false)
  const pipeline = editing.pipeline || ''   // a built-in deterministic pass, if any
  const save = async () => {
    if (!prompt.trim() || busy) return
    setBusy(true)
    const list = s.presets.slice()
    const fields = { name: name.trim() || 'Untitled', desc: desc.trim(), prompt }
    if (isNew) {
      list.push({ id: 'p' + Math.random().toString(36).slice(2, 10), ...fields })
    } else {
      const i = list.findIndex((p) => p.id === editing.id)
      if (i >= 0) list[i] = { ...list[i], ...fields }
    }
    try { await s.savePresets(list); close() } catch { /* save failed → keep the modal open so nothing typed is lost */ } finally { setBusy(false) }
  }
  const del = async () => {
    if (busy) return
    setBusy(true)
    try { await s.savePresets(s.presets.filter((p) => p.id !== editing.id)); close() } catch { /* keep open */ } finally { setBusy(false) }
  }
  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="modal-card">
        <div className="modal-head">
          <div className="modal-title">{isNew ? 'New preset' : 'Edit preset'}</div>
          <span className="modal-x" onClick={close}>✕</span>
        </div>
        <div className="modal-body">
          <div className="modal-sub">
            A preset is a one-click prompt. In the chat, click the chip to run it; shift+click to drop it
            in the box and look it over before sending.
          </div>
          <input className="preset-name" placeholder="Preset name (e.g. First edit)"
                 value={name} onChange={(e) => setName(e.target.value)} />
          <input className="preset-name" maxLength={160}
                 placeholder="What it does, in one sentence (shown when you hover the chip)"
                 value={desc} onChange={(e) => setDesc(e.target.value)} />
          <textarea className="prefs-text" placeholder="The preset text — exactly what you would type in the chat."
                    value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <div className="chat-hint" style={{ marginTop: 8 }}>
            {/* stating this under a preset that declares it needs no script read as a
                straight contradiction — the built-in clean pass is exactly that case */}
            {'Every edit needs an approved script (Script tab) — the AI writes it, you approve it, '
              + 'and it carries on by itself.'}
            {pipeline === 'cleanup' && ' After you approve, this one runs the built-in clean pass '
              + 'directly: it drops the errors and the dead air and keeps every good take.'}
          </div>
          <div className="modal-actions">
            {isNew ? <span /> : <button className="btn-modal danger" onClick={del} disabled={busy}>Delete</button>}
            <button className="btn-modal primary" onClick={save} disabled={busy || !prompt.trim()}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

export function ExportModal({ s }) {
  const { project: p, exp, startExport, setModal } = s
  const close = () => setModal(null)
  const [showLog, setShowLog] = useState(false)

  // honest heads-up: muted/hidden tracks won't be in the export (REN-112)
  const warnings = []
  for (const [k, v] of Object.entries(p.tracks || {})) {
    if (v?.hidden) warnings.push(`${TRACK_NAMES[k] || k} track is hidden — it won't be in the export`)
    if (v?.muted) warnings.push(`${TRACK_NAMES[k] || k} track is muted — its audio won't be in the export`)
  }

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="modal-card">
        <div className="modal-head">
          <div className="modal-title">Export video</div>
          <span className="modal-x" onClick={close}>✕</span>
        </div>

        {exp.phase === 'choose' && warnings.length > 0 && (
          <div className="export-warns">
            {warnings.map((w, i) => <div key={i} className="export-warn">⚠ {w}</div>)}
          </div>
        )}

        {exp.phase === 'choose' && (
          <div className="opt-cards">
            <div className="opt-card violet" onClick={() => startExport('final')}>
              <b>Final quality</b>
              <span>{p.w}×{p.h} · project native resolution · full-res source</span>
            </div>
            <div className="opt-card" onClick={() => startExport('fast')}>
              <b>Quick test</b>
              <span>720p · lighter render to check before posting</span>
            </div>
          </div>
        )}

        {exp.phase === 'rendering' && (
          <div className="exp-rendering">
            <div className="row">
              <span className="l">rendering {exp.quality === 'fast' ? '720p (test)' : `${p.w}×${p.h} (final)`}…</span>
              <span className="r">{exp.pct}%</span>
            </div>
            <div className="progress-bar"><div style={{ width: `${exp.pct}%` }} /></div>
            <div className="note">You can keep editing — the render runs in the background.</div>
          </div>
        )}

        {exp.phase === 'done' && (
          <div className="exp-done">
            <div className="done-circle">✓</div>
            <div className="exp-filename">{exp.filename}</div>
            <div className="exp-btns">
              <button className="btn-modal" onClick={() => api.reveal(exp.out)}>Show in Finder</button>
              <button className="btn-modal" onClick={() => api.openFile(exp.out)}>Open video</button>
              <button className="btn-modal primary" onClick={close}>Close</button>
            </div>
          </div>
        )}

        {exp.phase === 'error' && (
          <div className="exp-done">
            <div className="err-circle">✕</div>
            <div className="exp-filename">Export failed.</div>
            <button className="err-toggle" onClick={() => setShowLog(!showLog)}>
              {showLog ? 'hide log' : 'show log'}
            </button>
            {showLog && <pre className="err-log">{(exp.log || []).join('\n') || 'no log output'}</pre>}
            <div className="exp-btns">
              <button className="btn-modal primary" onClick={() => startExport(exp.quality)}>Retry</button>
              <button className="btn-modal" onClick={close}>Close</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const TRANSCRIBE_LANGS = [
  ['auto', 'Auto-detect', 'let the model figure out the spoken language'],
  ['en', 'English', ''],
  ['pt', 'Portuguese', ''],
  ['de', 'German', ''],
  ['es', 'Spanish', ''],
]

export function TranscribeModal({ s }) {
  const { transc, startTranscribe, setModal } = s
  const close = () => setModal(null)
  const [showLog, setShowLog] = useState(false)

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="modal-card">
        <div className="modal-head">
          <div className="modal-title">Auto captions</div>
          <span className="modal-x" onClick={close}>✕</span>
        </div>

        {transc.phase === 'choose' && (
          <>
            <div className="modal-sub">
              {s.project?.captions?.length
                ? 'Re-transcribe the CURRENT cut — replaces the existing captions with fresh word-synced ones from exactly what is on the timeline now (your chosen takes and cuts). '
                : 'Transcribe the voice and add word-synced karaoke captions from the current timeline. '}
              Uses the most accurate local model available. Pick the spoken language:
            </div>
            <div className="opt-cards">
              {TRANSCRIBE_LANGS.map(([code, label, hint], i) => (
                <div key={code} className={`opt-card lang ${i === 0 ? 'violet' : ''}`}
                     onClick={() => startTranscribe(code)}>
                  <b>{label}</b>
                  {hint && <span>{hint}</span>}
                </div>
              ))}
            </div>
          </>
        )}

        {transc.phase === 'running' && (
          <div className="exp-rendering">
            <div className="row">
              <span className="l">transcribing the timeline voice…</span>
              <span className="r">{transc.pct}%</span>
            </div>
            <div className="progress-bar"><div style={{ width: `${transc.pct}%` }} /></div>
            <div className="note">Captions land on the timeline when it finishes — you can keep editing.</div>
          </div>
        )}

        {transc.phase === 'done' && (
          <div className="exp-done">
            <div className="done-circle">✓</div>
            <div className="exp-filename">{transc.added} caption{transc.added === 1 ? '' : 's'} added</div>
            <div className="exp-btns">
              <button className="btn-modal primary" onClick={close}>Close</button>
            </div>
          </div>
        )}

        {transc.phase === 'error' && (
          <div className="exp-done">
            <div className="err-circle">✕</div>
            <div className="exp-filename">Transcription failed.</div>
            <button className="err-toggle" onClick={() => setShowLog(!showLog)}>
              {showLog ? 'hide log' : 'show log'}
            </button>
            {showLog && <pre className="err-log">{(transc.log || []).join('\n') || 'no log output'}</pre>}
            <div className="exp-btns">
              <button className="btn-modal primary" onClick={s.openTranscribe}>Try again</button>
              <button className="btn-modal" onClick={close}>Close</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export function HistoryModal({ s }) {
  const { snapshots, restoreSnapshot, setModal } = s
  const close = () => setModal(null)

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="modal-card history">
        <div className="modal-head">
          <div className="modal-title">Version history</div>
          <span className="modal-x" onClick={close}>✕</span>
        </div>
        <div className="modal-sub">Everything auto-saves as you edit. Restoring keeps the current version as a snapshot first.</div>
        <div className="hist-list">
          {snapshots.slice(0, 14).map((sn) => (
            <div key={sn.file} className="hist-row" onClick={() => restoreSnapshot(sn.file)}>
              <div className="l">
                <div className="hist-label">{sn.label}</div>
                <div className="hist-when">{fmtWhen(sn.ts * 1000)}</div>
              </div>
              <div className={`hist-by ${sn.author === 'claude' ? 'claude' : ''}`}>
                {sn.author === 'claude' ? 'AI' : 'You'}
              </div>
            </div>
          ))}
          {!snapshots.length && <div className="hist-empty">No snapshots yet — edits auto-save as you go.</div>}
        </div>
      </div>
    </div>
  )
}

// Script ready (REN-141): the preset writes the script in the background and
// nothing used to say so — he found out by going to look. This interrupts once,
// with the script editable in place, so approving is one click from wherever he
// was. Editing here revokes approval exactly like editing in the Script tab.
export function ScriptAlertModal({ s }) {
  const [draft, setDraft] = useState(s.scriptAlert || '')
  const [busy, setBusy] = useState(false)
  const close = () => s.setScriptAlert(null)

  const approve = async () => {
    setBusy(true)
    try {
      if (draft.trim() !== (s.scriptAlert || '').trim()) {
        s.mutate((p) => { p.script = draft; p.scriptApproved = false }, false)
      }
      close()
      await s.approveScript()
    } catch (e) {
      s.showToast?.(`Could not approve: ${e.message || e}`)
    } finally { setBusy(false) }
  }

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="modal-card">
        <div className="modal-head">
          <div className="modal-title">Script ready</div>
          <span className="modal-x" onClick={close}>✕</span>
        </div>
        <div className="modal-body">
          <div className="modal-sub">
            Review it. On approve, the AI cuts the video using exactly these lines —
            one line per cut.
          </div>
          <textarea className="script-text" value={draft} rows={14}
                    onChange={(e) => setDraft(e.target.value)} />
          <div className="modal-actions">
            <button className="btn-ghost" onClick={close} disabled={busy}>Edit later</button>
            <button className="btn-primary" onClick={approve} disabled={busy || !draft.trim()}>
              {busy ? 'Approving…' : '✓ Approve script'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
