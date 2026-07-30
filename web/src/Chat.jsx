import { useEffect, useRef, useState } from 'react'

// Model picker (REN-118). Keys match the server's CHAT_MODELS allowlist.
const EFFORTS = [
  { id: 'low', label: 'Low' },
  { id: 'medium', label: 'Medium' },
  { id: 'high', label: 'High' },
  { id: 'max', label: 'Max' },
  { id: 'ultracode', label: 'Ultracode' },
]
const modelLabel = (key, models) => (models || []).find((m) => m.key === key)?.label || 'AI'

export default function Chat({ s, embedded = false }) {
  const endRef = useRef(null)
  const [scanning, setScanning] = useState(false)
  const rows = Math.min(4, Math.max(2, Math.ceil(s.chatInput.length / 34)))

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [s.chatMsgs.length, s.claudeTyping])

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      s.sendChat()
    }
  }

  return (
    <div className={`chat ${s.isMobile ? 'mobile' : ''} ${embedded ? 'embedded' : ''}`}>
      {!embedded && (
        <div className="chat-header">
          <span className="chat-dot" />
          <div className="chat-title">Claude</div>
          <div className="chat-badge">{modelLabel(s.chatModel, s.models)}</div>
          <div className="spacer" />
          {s.isMobile && <span className="chat-close" onClick={() => s.setChatOpen(false)}>✕</span>}
        </div>
      )}
      <div className="chat-msgs">
        {s.chatMsgs.map((m, i) => (
          <div key={i} className={`chat-msg ${m.who === 'claude' ? 'claude' : 'you'}`}>
            <div className="chat-who">{m.who === 'claude' ? 'Claude' : 'you'}</div>
            <div className="chat-bubble">{m.text}</div>
          </div>
        ))}
        {s.claudeTyping && (
          <div className="typing"><span /><span /><span /></div>
        )}
        <div ref={endRef} />
      </div>
      <div className="chat-composer">
        <div className="chat-presets">
          {(s.presets || []).map((ps) => (
            <span key={ps.id} className={`preset-chip ${s.claudeTyping ? 'busy' : ''}`}
                  onClick={(e) => {
                    if (s.claudeTyping) return // an edit is already running
                    e.shiftKey ? s.setChatInput(ps.prompt) : s.sendChat(ps.prompt, ps.id)
                  }}>
              {ps.name}
              <span className="preset-edit" title="Edit preset"
                    onClick={(e) => { e.stopPropagation(); s.setEditingPreset(ps); s.setModal('preset') }}>✎</span>
              {/* own tooltip: a native title() is too slow and too cramped for a sentence */}
              <span className="preset-tip">
                <b>{ps.desc || ps.name}</b>
                click: run · shift+click: edit in chat
              </span>
            </span>
          ))}
          <span className="preset-chip new" title="New preset"
                onClick={() => { s.setEditingPreset({}); s.setModal('preset') }}>＋ Preset</span>
        </div>
        <textarea
          value={s.chatInput}
          onChange={(e) => s.setChatInput(e.target.value)}
          onKeyDown={onKeyDown}
          rows={rows}
          placeholder='Ask for a revision… e.g. “make the captions bigger and trim the silence in take 2”'
        />
        <div className="chat-controls">
          <label className="chat-pick" title="Which Claude model runs the edit">
            <span>Model</span>
            <select value={s.chatModel} onChange={(e) => s.setChatModel(e.target.value)}>
              {(s.models || []).map((m) => (
                <option key={m.key} value={m.key}>{m.label}</option>
              ))}
              {!(s.models || []).some((m) => m.key === s.chatModel) && (
                <option value={s.chatModel}>{s.chatModel}</option>
              )}
            </select>
          </label>
          <label className="chat-pick" title="How much the model thinks before editing (higher = slower, more careful)">
            <span>Effort</span>
            <select value={s.chatEffort} onChange={(e) => s.setChatEffort(e.target.value)}>
              {EFFORTS.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
            </select>
          </label>
          <button type="button" className="chat-prefs" title="Scan the installed CLIs for new models"
                  onClick={() => { setScanning(true); s.rescanModels().finally(() => setTimeout(() => setScanning(false), 4000)) }}>
            {scanning ? '⟳…' : '⟳'}
          </button>
          <button type="button" className="chat-prefs" onClick={() => s.setModal('prefs')}
                  title="Editor preferences — the style the AI has learned (and you can edit)">⚙ Style</button>
          <div className="spacer" />
          <button className="btn-send" onClick={s.sendChat}>Send ⏎</button>
        </div>
      </div>
    </div>
  )
}
