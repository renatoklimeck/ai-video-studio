import { useEffect, useRef, useState } from 'react'

const PIN_LEN = 4

export default function PinGate({ onUnlock }) {
  const [pin, setPin] = useState('')
  const [error, setError] = useState(null)     // 'wrong' | 'locked'
  const [retryIn, setRetryIn] = useState(0)
  const [busy, setBusy] = useState(false)
  const pinRef = useRef(pin)
  pinRef.current = pin

  async function submit(value) {
    setBusy(true)
    setError(null)
    try {
      const r = await fetch('/api/auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: value }),
      })
      if (r.ok) { onUnlock(); return }
      const data = await r.json().catch(() => ({}))
      if (r.status === 429) {
        setError('locked')
        setRetryIn(data.retryAfter || 30)
      } else {
        setError('wrong')
      }
      setPin('')
    } catch {
      setError('wrong')
      setPin('')
    } finally {
      setBusy(false)
    }
  }

  const push = (d) => {
    if (busy || retryIn > 0) return
    setError(null)
    const next = (pinRef.current + d).slice(0, PIN_LEN)
    setPin(next)
    if (next.length === PIN_LEN) submit(next)
  }
  const pop = () => { setError(null); setPin((p) => p.slice(0, -1)) }

  useEffect(() => {
    const onKey = (e) => {
      if (/^[0-9]$/.test(e.key)) push(e.key)
      if (e.key === 'Backspace') pop()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, retryIn])

  useEffect(() => {
    if (retryIn <= 0) return
    const iv = setInterval(() => setRetryIn((s) => {
      if (s <= 1) { setError(null); return 0 }
      return s - 1
    }), 1000)
    return () => clearInterval(iv)
  }, [retryIn > 0])

  return (
    <div className="pin-gate">
      <div className={`pin-card ${error === 'wrong' ? 'shake' : ''}`}>
        <div className="logo-tile lg"><div className="tri" /></div>
        <div className="pin-title">AI Video Studio</div>
        <div className="pin-sub">Enter PIN</div>
        <div className="pin-dots">
          {Array.from({ length: PIN_LEN }, (_, i) => (
            <span key={i} className={`pin-dot ${i < pin.length ? 'on' : ''}`} />
          ))}
        </div>
        <div className="pin-msg">
          {error === 'wrong' && <span className="err">Wrong PIN</span>}
          {retryIn > 0 && <span className="err">Too many attempts — try again in {retryIn}s</span>}
        </div>
        <div className="pin-pad">
          {['1', '2', '3', '4', '5', '6', '7', '8', '9', '', '0', '⌫'].map((k, i) => (
            k === ''
              ? <span key={i} />
              : <button key={i} disabled={busy || retryIn > 0}
                        onClick={() => (k === '⌫' ? pop() : push(k))}>{k}</button>
          ))}
        </div>
      </div>
    </div>
  )
}
