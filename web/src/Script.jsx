import { useState, useEffect, useRef } from 'react'

// Script tab (REN-123): the clean intended script the AI deduces from the raw
// recording — the best/final version of each line, in order. Editable; used as
// the reference for take selection, and can be applied to the captions (REN-124).
export default function Script({ s }) {
  const p = s.project
  const pid = p?._dir // project identity — resync when switching projects
  const taRef = useRef(null)
  const [busy, setBusy] = useState('') // '' | 'gen' | 'apply' | 'approve'
  const [err, setErr] = useState('')
  const [draft, setDraft] = useState(p?.script || '')
  const hasCaptions = (p?.captions?.length || 0) > 0
  // while the AI edits (chat job running), lock this tab — editing the script
  // mid-run would revoke approval / clobber the in-flight edit
  const locked = !!busy || s.claudeTyping
  // resync on project switch or when the STORED script changes (generate /
  // external write) — but never while the user is actively typing.
  useEffect(() => {
    if (taRef.current && document.activeElement === taRef.current) return
    setDraft(p?.script || '')
  }, [pid, p?.script])

  // editing the script revokes approval — the AI must only edit from a script
  // the creator re-approved after the change (REN-127)
  const save = () => {
    if (draft !== (p?.script || '')) s.mutate((pp) => { pp.script = draft; pp.scriptApproved = false }, false)
  }
  // server-side approval; if an edit was waiting for it, it resumes automatically
  const approve = () => run('approve', s.approveScript, 'Generate or write the script first.')
  const approved = !!p?.scriptApproved && !!p?.script
  const run = async (kind, fn, msg400) => {
    setBusy(kind); setErr('')
    try { await fn() }
    catch (e) {
      const m = String(e?.message || e)
      setErr(/\b409\b/.test(m) ? 'The AI is already working on this project — wait for it to finish.'
        : /\b400\b/.test(m) ? msg400
        : `Falhou: ${m}`)
    } finally { setBusy('') }
  }

  return (
    <div className="script-tab">
      <div className="script-head">
        <div className="script-sub">
          The clean script the AI works out from the video — the best version of every line, in order.
          Review it, edit it and <b> approve</b>: the first cut only runs on an approved script, and follows
          it exactly. Fixed a spelling, a number or a word afterwards? “Apply to captions” updates the
          captions and keeps them in sync.
        </div>
        {p?.script && (
          <div className={`script-status ${approved ? 'ok' : ''}`}>
            {approved ? '✓ Script approved — the AI will follow it'
              : 'Draft — review and approve it to unlock the edit'}
          </div>
        )}
        <div className="script-actions">
          <button className="tx-transcribe" disabled={locked} onClick={() => run('gen', s.generateScript, 'Transcribe the video first (Cut tab).')}>
            {busy === 'gen' ? 'Generating…' : (p?.script ? 'Regenerate from video' : 'Generate from video')}
          </button>
          {p?.script && !approved && (
            <button className="tx-transcribe approve" disabled={locked} onClick={approve}
                    title="Approve this script — if an edit was waiting on it, that edit carries on by itself">
              {busy === 'approve' ? 'Approving…' : '✓ Approve script'}
            </button>
          )}
          <button className="tx-transcribe alt" disabled={locked || !p?.script || !hasCaptions}
                  title={!p?.script ? 'Generate or write the script first'
                    : !hasCaptions ? 'Transcribe the video first (Cut tab)'
                    : 'Corrige o texto da legenda pra bater com o roteiro, sem mexer no tempo'}
                  onClick={() => run('apply', s.applyScriptToCaptions, 'Precisa de roteiro e de legenda (transcreva na aba Cut).')}>
            {busy === 'apply' ? 'Applying…' : 'Apply to captions'}
          </button>
        </div>
        {err && <div className="script-err">⚠ {err}</div>}
      </div>
      <textarea
        ref={taRef}
        className="script-text"
        value={draft}
        disabled={locked}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        placeholder="Click “Generate from video” to have the AI build the clean script — or write your own here."
      />
    </div>
  )
}
