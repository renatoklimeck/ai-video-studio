import Chat from './Chat'
import Transcript from './Transcript'
import Script from './Script'

// Left panel tabs: Claude chat | Script (clean intended script, REN-123) |
// Cut (Descript-style text editing — delete words to cut the video, REN-83)
export default function LeftPanel({ s }) {
  const tab = s.leftTab
  return (
    <div className={`leftpanel ${s.isMobile ? 'mobile' : ''}`}>
      <div className="lp-tabs">
        {/* The tab NAMES THE ENGINE that will run the next edit, so it reads
            "Codex" for a Codex user. 'claude' stays as the tab id — that is a
            wire value, not a brand. */}
        <button className={tab === 'claude' ? 'on' : ''} onClick={() => s.setLeftTab('claude')}>
          <span className="claude-dot" />{s.engineName}
        </button>
        <button className={tab === 'script' ? 'on' : ''} onClick={() => s.setLeftTab('script')}>
          Script
        </button>
        <button className={tab === 'transcript' ? 'on' : ''} onClick={() => s.setLeftTab('transcript')}>
          Cut
        </button>
        <div className="spacer" />
        {s.isMobile && <span className="lp-close" onClick={() => s.setChatOpen(false)}>✕</span>}
      </div>
      {tab === 'claude' ? <Chat s={s} embedded /> : tab === 'script' ? <Script s={s} /> : <Transcript s={s} />}
    </div>
  )
}
