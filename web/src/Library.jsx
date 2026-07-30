import { useRef, useState } from 'react'
import { ago, fmtTime } from './time'
import UpdateButton from './UpdateButton'

export default function Library({ s }) {
  const fileRef = useRef(null)
  const [dropOver, setDropOver] = useState(false)

  const onFilePicked = (e) => {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (f) s.importNewProject(f)
  }

  const isFileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
  const onDragOver = (e) => { if (isFileDrag(e)) { e.preventDefault(); setDropOver(true) } }
  const onDragLeave = (e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDropOver(false) }
  const onDrop = (e) => {
    if (!isFileDrag(e)) return
    e.preventDefault(); setDropOver(false)
    s.dropMedia(e.dataTransfer.files, { target: 'library' })
  }

  return (
    <div className={`library ${dropOver ? 'dropping' : ''}`}
         onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
      {dropOver && <div className="lib-drop-hint"><div className="box">Drop a video to start a new project</div></div>}
      {s.toast && <div className="vs-toast">{s.toast}</div>}
      <div className="library-col">
        <div className="brand-row">
          <div className="logo-tile lg"><div className="tri" /></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <div className="brand-title">AI Video Studio</div>
            <div className="brand-sub">fine-tuning console · Claude ⇄ you</div>
          </div>
          {/* also here, not just in the editor: this is the screen you land on */}
          <div style={{ marginLeft: 'auto' }}><UpdateButton /></div>
        </div>

        <div className="lib-section">
          <div className="lib-label">New project</div>
          <div className="new-card">
            <div>
              <h3>Import your source video</h3>
              <p>Aspect ratio (9:16 or 16:9) and duration are detected from the file.</p>
            </div>
            <div className="actions">
              {s.importing && <div className="reading">reading file…</div>}
              <button className="btn-choose" onClick={() => fileRef.current?.click()}>Choose video…</button>
              <a className="start-empty" onClick={s.startEmpty}>or start empty</a>
            </div>
          </div>
          <input ref={fileRef} type="file" accept="video/*" hidden onChange={onFilePicked} />
        </div>

        <div className="lib-section">
          <div className="lib-label">Projects</div>
          {s.library.map((pr) => {
            const port = pr.aspect !== '16:9'
            return (
              <div key={pr.id} className="proj-card" onClick={() => s.openProject(pr.id)}>
                <div className="left">
                  {pr.thumb
                    ? <img className="proj-thumb" src={`${pr.thumb}&v=${pr.editedAt}`} alt=""
                           style={{ width: port ? 34 : 60, height: port ? 60 : 34 }} />
                    : <div className="proj-thumb" style={{ width: port ? 34 : 60, height: port ? 60 : 34 }} />}
                  <div className="info">
                    <div className="proj-name">{pr.name}</div>
                    <div className="proj-meta">{pr.w}×{pr.h} · {pr.aspect} · {fmtTime(pr.dur)} · {pr.clips} clips</div>
                  </div>
                </div>
                <div className="right">
                  <div className={`proj-by ${pr.by === 'claude' ? 'claude' : ''}`}>
                    {pr.by === 'claude' ? 'Claude edit' : 'yours'} · {ago(pr.editedAt)}
                  </div>
                  <button className="card-btn" title="Duplicate"
                          onClick={(e) => { e.stopPropagation(); s.duplicateProject(pr.id) }}>⧉</button>
                  <button className="card-btn del" title="Delete"
                          onClick={(e) => { e.stopPropagation(); s.removeProject(pr.id, pr.name) }}>✕</button>
                </div>
              </div>
            )
          })}
          {!s.library.length && (
            <div className="lib-empty">
              <b>No projects yet</b>
              <span>Import a video above, or ask Claude to assemble the first cut — it will show up here.</span>
            </div>
          )}
        </div>

        <div className="lib-footer">Each project is a project.json shared between you and Claude — both edit the same file.</div>
      </div>
    </div>
  )
}
