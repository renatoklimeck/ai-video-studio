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

  const [ask, setAsk] = useState(null)   // the "there is a new version" prompt

  useEffect(() => {
    let dead = false
    const check = async () => {
      try {
        const r = await api.updateCheck()
        if (!dead && r.supported && r.behind > 0) {
          setState(r)
          // Ask once per version. A chip in the corner is easy to never notice,
          // and someone who never updates is running the bugs we already fixed;
          // asking every launch would be nagging, so the answer is remembered
          // against the version it was given for.
          // keyed on the version name when there is one (REN-168): the commit
          // subject can repeat between releases, and then "Not now" would
          // silence a genuinely new version.
          const key = 'vs-update-asked'
          const seen = r.latestVersion || r.latest
          if (localStorage.getItem(key) !== seen) setAsk({ ...r, key, seen })
        }
      } catch { /* offline — the app works fine without this */ }
    }
    check()
    const iv = setInterval(check, 30 * 60 * 1000)   // twice an hour is plenty
    return () => { dead = true; clearInterval(iv) }
  }, [])

  const run = async () => {
    setAsk(null)
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

  // Whether to lead with the version NAME. Normally yes. But the offer is
  // driven by a COMMIT count, not by the version, so a fix pushed between two
  // releases is offered while VERSION has not moved — and then the box read
  // "2026.08.04.8 — you have 2026.08.04.8", which is nonsense. In that case
  // lead with what actually changed instead.
  const named = (a) => !!a?.latestVersion && a.latestVersion !== a.version

  return (
    <div className="updater">
      {ask && phase === 'idle' && (
        <div className="update-ask-back" onClick={() => setAsk(null)}>
          <div className="update-ask" onClick={(e) => e.stopPropagation()}>
            <h3>A new version is available</h3>
            {/* the version NAME first (REN-168) — "2026.08.04.2" is what the
                header will show afterwards, so it is what to compare against.
                Checkouts older than the VERSION file have none: fall back to
                the commit subject rather than showing an empty box. */}
            {named(ask)
              ? <p className="update-ask-what">
                  <span className="update-ask-ver">{ask.latestVersion}</span>
                  {ask.version ? <span className="update-ask-from"> — you have {ask.version}</span> : null}
                </p>
              : <p className="update-ask-what">{ask.latest}</p>}
            {named(ask) && ask.latest && <p className="update-ask-sub">{ask.latest}</p>}
            <p className="update-ask-note">
              It takes about a minute. Your projects are not touched, and if
              anything fails it puts the working version back.
            </p>
            <div className="update-ask-actions">
              <button className="btn-modal" onClick={() => {
                localStorage.setItem(ask.key, ask.seen || '')
                setAsk(null)
              }}>Not now</button>
              <button className="btn-modal primary" onClick={run}>Update now</button>
            </div>
          </div>
        </div>
      )}
      {phase === 'idle' && (
        <button className="btn-update" onClick={run}
                title={named(state) ? `Update to ${state.latestVersion}`
                     : state.latest ? `Latest: ${state.latest}` : 'Install the new version'}>
          {/* the count is its own element so CSS can drop it on a narrow phone.
              `behind` is a raw commit count, so a student who has not updated
              in a while sits at two or three digits, and "↑ Update (12)" is
              wide enough to shave the ☰ off the right edge of a 320px screen.
              Capping it at "9+" does not help — that string is WIDER than
              "12" in this font (measured: 99.8px vs 97.1px). */}
          ↑ Update{state.behind > 1 ? <span className="update-count"> ({state.behind})</span> : null}
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
