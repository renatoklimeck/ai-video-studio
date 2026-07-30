import { useEffect, useRef, useState } from 'react'
import { authStatus } from './api'
import { useStudio } from './store'
import Library from './Library'
import Editor from './Editor'
import PinGate from './PinGate'

// PIN gate wrapper: when the server requires auth (VPS), nothing renders until
// the session cookie is valid. The local server (no PIN file) skips all this.
export default function App() {
  const [auth, setAuth] = useState(null) // null = checking · true = go · false = PIN
  useEffect(() => {
    authStatus()
      .then((a) => setAuth(a.authed))
      .catch(() => setAuth(true)) // server unreachable: let the app show its own errors
    const onNeed = () => setAuth(false)
    window.addEventListener('vs-auth-required', onNeed)
    return () => window.removeEventListener('vs-auth-required', onNeed)
  }, [])
  if (auth === null) return null
  if (auth === false) return <PinGate onUnlock={() => window.location.reload()} />
  return <Studio />
}

function Studio() {
  const s = useStudio()
  const sRef = useRef(s)
  sRef.current = s

  // Keyboard (desktop): Space play/pause · Cmd/Ctrl+Z undo · Shift+Cmd/Ctrl+Z redo ·
  // Delete/Backspace delete selection · F fullscreen · Esc exit/close.
  // Cmd+S is intentionally unbound — everything auto-saves.
  useEffect(() => {
    const onKey = (e) => {
      const st = sRef.current
      const tag = (e.target.tagName || '').toLowerCase()
      const typing = tag === 'input' || tag === 'textarea' || tag === 'select' ||
        e.target.isContentEditable
      if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); return }
      if (st.screen !== 'editor') return
      if (e.code === 'Space' && !typing) { e.preventDefault(); st.setPlaying((p) => !p) }
      if ((e.key === 'f' || e.key === 'F') && !typing && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); st.setFullscreen((f) => !f)
      }
      if (e.key === 'Escape' && !e.target.isContentEditable) {
        // while inline-editing, Esc only commits (handled by the element itself)
        st.setFullscreen(false); st.setModal(null); st.setMenuOpen(false)
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z') && !typing) {
        e.preventDefault()
        if (e.shiftKey) st.redo(); else st.undo()
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') && !typing && st.sel) {
        e.preventDefault(); st.deleteSel()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (s.screen === 'projects') return <Library s={s} />
  return <Editor s={s} />
}
