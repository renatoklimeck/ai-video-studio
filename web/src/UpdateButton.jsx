import { useEffect, useRef, useState } from 'react'
import { api } from './api'

// In-app update (no terminal after day one). The heavy install is 4 GB of
// dependencies that never change; an update is ~1 MB of code, so this is fast.
//
// The chip only appears when there IS something to install — a permanent
// "you're up to date" badge is noise in a header you look at all day.
export default function UpdateButton() {
  const [state, setState] = useState(null)   // {behind, latest} | null
  const [phase, setPhase] = useState('idle') // idle | running | failed
  const [lines, setLines] = useState([])
  const poll = useRef(null)

  useEffect(() => {
    let dead = false
    const check = async () => {
      try {
        const r = await api.updateCheck()
        if (!dead && r.supported && r.behind > 0) setState(r)
      } catch { /* offline — the app works fine without this */ }
    }
    check()
    const iv = setInterval(check, 30 * 60 * 1000)   // twice an hour is plenty
    return () => { dead = true; clearInterval(iv) }
  }, [])

  const run = async () => {
    setPhase('running')
    try { await api.updateRun() } catch { setPhase('failed'); return }
    // Keep polling THROUGH the restart at the end: the requests that fail while
    // the server is down are expected, not an error.
    poll.current = setInterval(async () => {
      try {
        const r = await api.updateLog()
        setLines(r.lines || [])
        if (r.done === 'failed') { clearInterval(poll.current); setPhase('failed') }
        if (r.done === 'ok') {
          // The server is back (this request just succeeded). The store already
          // knows how to reload safely — it flushes unsaved edits first — and it
          // does that check on window focus. Poking that is better than calling
          // reload() here and racing a pending save. Without this the page sits
          // on "restarting" until the store's own 60s tick comes around.
          window.dispatchEvent(new Event('focus'))
        }
      } catch { /* server is restarting */ }
    }, 1000)
  }

  useEffect(() => () => clearInterval(poll.current), [])

  if (!state && phase === 'idle') return null

  return (
    <div className="updater">
      {phase === 'idle' && (
        <button className="btn-update" onClick={run}
                title={state.latest ? `Latest: ${state.latest}` : 'Install the new version'}>
          ↑ Update{state.behind > 1 ? ` (${state.behind})` : ''}
        </button>
      )}
      {phase === 'running' && (
        <div className="updater-pop">
          <div className="updater-head">Updating…</div>
          {/* the log also carries raw npm and git output — useful in the file,
              noise in a popup. Show the phases and the plain-language notes. */}
          {lines.filter((l) => l.startsWith('STEP') || l.trim().startsWith('('))
                .slice(-4).map((l, i) => (
            <div key={i} className="updater-line">{l.replace(/^STEP /, '')}</div>
          ))}
          <div className="updater-note">The page reloads by itself when it is done.</div>
        </div>
      )}
      {phase === 'failed' && (
        <div className="updater-pop">
          <div className="updater-head">Update failed</div>
          {lines.filter((l) => l.startsWith('ERROR')).slice(-2).map((l, i) => (
            <div key={i} className="updater-line">{l.replace(/^ERROR /, '')}</div>
          ))}
          <div className="updater-note">Nothing was changed. Your projects are untouched.</div>
        </div>
      )}
    </div>
  )
}
