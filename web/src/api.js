const j = (r) => {
  if (r.status === 401) {
    window.dispatchEvent(new Event('vs-auth-required'))
    throw new Error('HTTP 401')
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json()
}

export const authStatus = () => fetch('/api/auth').then((r) => r.json())

export const api = {
  projects: () => fetch('/api/projects').then(j),
  create: (name) => fetch('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(j),
  importProject: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch('/api/projects/import', { method: 'POST', body: fd }).then(j)
  },
  importTake: (pid, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch(`/api/project/${pid}/take`, { method: 'POST', body: fd }).then(j)
  },
  duplicate: (pid) => fetch(`/api/project/${pid}/duplicate`, { method: 'POST' }).then(j),
  remove: (pid) => fetch(`/api/project/${pid}`, { method: 'DELETE' }).then(j),
  get: (pid) => fetch(`/api/project/${pid}`).then(j),
  mtime: (pid) => fetch(`/api/project/${pid}/mtime`).then(j),
  save: (pid, project) => {
    const body = {}
    for (const [k, v] of Object.entries(project)) if (!k.startsWith('_')) body[k] = v
    body._by = 'you'
    return fetch(`/api/project/${pid}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j)
  },
  history: (pid) => fetch(`/api/project/${pid}/history`).then(j),
  restore: (pid, file) => fetch(`/api/project/${pid}/restore`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file }),
  }).then(j),
  export: (pid, quality) => fetch(`/api/project/${pid}/export`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ quality }),
  }).then(j),
  segbg: (pid, clipId) => fetch(`/api/project/${pid}/segbg`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clipId }),
  }).then(j),
  retouch: (pid, clipId) => fetch(`/api/project/${pid}/retouch`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clipId }),
  }).then(j),
  facedetect: (pid, path) => fetch(`/api/project/${pid}/facedetect`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  facetrack: (pid, path) => fetch(`/api/project/${pid}/facetrack?path=${encodeURIComponent(path)}`).then(j),
  cutout: (pid, path) => fetch(`/api/project/${pid}/cutout`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  proxy: (pid, path) => fetch(`/api/project/${pid}/proxy`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  job: (jid) => fetch(`/api/job/${jid}`).then(j),
  reveal: (path) => fetch('/api/reveal', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  openFile: (path) => fetch('/api/open', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  strip: (pid, path) => fetch(`/api/project/${pid}/strip`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  wave: (pid, path) => fetch(`/api/project/${pid}/wave`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  srcwave: (pid, path) => fetch(`/api/project/${pid}/srcwave`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(j),
  transcribe: (pid, lang) => fetch(`/api/project/${pid}/transcribe`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lang }),
  }).then(j),
  transcribeSource: (pid, key, path, lang) => fetch(`/api/project/${pid}/transcribe_source`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, path, lang }),
  }).then(j),
  transcript: (pid, path) => fetch(`/api/project/${pid}/transcript?path=${encodeURIComponent(path)}`).then(j),
  chat: (pid) => fetch(`/api/project/${pid}/chat`).then(j),
  chatSend: (pid, message, model, effort, preset) => fetch(`/api/project/${pid}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, model, effort, preset }),
  }).then(j),
  upload: (pid, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch(`/api/project/${pid}/media`, { method: 'POST', body: fd }).then(j)
  },
  // editor preferences (REN-120): global style/rules the AI learns + you edit
  getPrefs: () => fetch('/api/preferences').then(j),
  putPrefs: (text) => fetch('/api/preferences', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }).then(j),
  // chat presets (REN-122): one-click prompts, user-editable
  generateScript: (pid, model) => fetch(`/api/project/${pid}/generate_script`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model }),
  }).then(j),
  approveScript: (pid) => fetch(`/api/project/${pid}/approve_script`, { method: 'POST' }).then(j),
  applyScriptToCaptions: (pid, model) => fetch(`/api/project/${pid}/apply_script_to_captions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model }),
  }).then(j),
  version: () => fetch('/api/version').then(j),
  updateCheck: () => fetch('/api/update/check').then(j),
  updateRun: () => fetch('/api/update', { method: 'POST' }).then(j),
  updateLog: () => fetch('/api/update/log').then(j),
  getEngines: () => fetch('/api/engines').then(j),
  getModels: () => fetch('/api/models').then(j),
  rescanModels: () => fetch('/api/models/rescan', { method: 'POST' }).then(j),
  getPresets: () => fetch('/api/presets').then(j),
  putPresets: (presets) => fetch('/api/presets', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ presets }),
  }).then(j),
}

export function fileUrl(project, p) {
  if (!p) return ''
  const abs = p.startsWith('/') ? p : `${project._dir}/${p}`
  return `/file?path=${encodeURIComponent(abs)}`
}

export async function pollJob(jid, onProgress) {
  if (jid == null) return { status: 'done' } // derived file already on disk
  // A long chat job polls for minutes; a single dropped request (brief network
  // blip, or the local server restarting mid-task) must NOT kill the whole chat
  // with "Load failed". Tolerate a run of consecutive failures before giving up.
  let fails = 0
  for (;;) {
    try {
      const st = await api.job(jid)
      fails = 0
      onProgress?.(st)
      if (st.status !== 'running') return st
    } catch (err) {
      // Only transport blips (fetch rejects with "TypeError: Load failed") are
      // worth waiting out. A definitive HTTP status (404 gone job, 401, 5xx) is
      // permanent — the in-memory JOBS dict never re-adds a missing id — so fail
      // fast instead of freezing every caller's progress bar for ~30s.
      if (/^HTTP 404/.test(err?.message || '')) {
        // The job id is gone, which on a local single-user app means one thing:
        // the server restarted while this was running (JOBS lives in memory).
        // Saying "Chat failed: HTTP 404" for that sends you hunting a bug in
        // your own edit — name what actually happened instead.
        const e = new Error('JOB_GONE')
        e.gone = true
        throw e
      }
      if (/^HTTP /.test(err?.message || '')) throw err
      if (++fails > 40) throw err // ~40 × 800ms ≈ 30s of transport failures → give up
    }
    await new Promise((r) => setTimeout(r, 800))
  }
}
