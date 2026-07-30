import { useEffect, useRef, useState } from 'react'

// Where the PREVIEW plays (REN-140). Deliberately unobtrusive: a speaker glyph
// in the header, not a control sitting in the transport.
//
// It only lists devices once the browser will name them, and the browser only
// names them after the page holds a media permission — so the prompt is here,
// behind an explicit click, with the reason spelled out. Nothing is recorded:
// the microphone track is stopped the instant permission is granted.
export default function AudioOut({ s }) {
  const [open, setOpen] = useState(false)
  const [asking, setAsking] = useState(false)
  const boxRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const away = (e) => { if (!boxRef.current?.contains(e.target)) setOpen(false) }
    window.addEventListener('pointerdown', away)
    return () => window.removeEventListener('pointerdown', away)
  }, [open])

  if (!s.sinkSupported) return null
  const current = s.sinks.find((d) => d.deviceId === s.sinkId)

  const reveal = async () => {
    setAsking(true)
    try { await s.loadSinks() } finally { setAsking(false) }
  }

  return (
    <div className="audioout" ref={boxRef}>
      <button className={`btn-icon ${s.sinkId ? 'on' : ''}`} title="Audio output"
              onClick={() => { setOpen(!open); if (!open && !s.sinks.length) reveal() }}>🔊</button>
      {open && (
        <div className="audioout-pop">
          <div className="audioout-head">Audio output</div>
          <button className={`audioout-row ${!s.sinkId ? 'sel' : ''}`}
                  onClick={() => { s.setSinkId(''); setOpen(false) }}>
            System output
          </button>
          {s.sinks.map((d) => (
            <button key={d.deviceId} className={`audioout-row ${d.deviceId === s.sinkId ? 'sel' : ''}`}
                    onClick={() => { s.setSinkId(d.deviceId); setOpen(false) }}>
              {d.label}
            </button>
          ))}
          {!s.sinks.length && (
            <div className="audioout-note">
              {asking ? 'asking the browser…'
                : 'The browser hides device names until this page is granted a media permission. Allow it once and the list appears — nothing is recorded.'}
            </div>
          )}
          {!s.sinks.length && !asking && (
            <button className="audioout-row" onClick={reveal}>Show my devices…</button>
          )}
          {current && <div className="audioout-note">now: {current.label}</div>}
        </div>
      )}
    </div>
  )
}
