// Does the app actually RUN, or does it just compile? (REN-204)
//
// `vite build` proves the syntax is valid. Twice in one day a release compiled
// perfectly and served a white screen — both times the same shape of bug: a
// hook's dependency array naming a `const` declared further down the file.
// That is a TDZ error, it throws during render, React unmounts the whole tree,
// and you get an empty <div id="root">. Nothing in the release path noticed,
// so it went out to students.
//
// This mounts the real app in jsdom against a stubbed-healthy API and fails if
// anything threw or if the root came out empty. Runs in about two seconds.
//
// Note: it bundles the source to IIFE rather than loading web/dist, because
// jsdom does not execute ES modules and Vite emits type="module". Same source
// files, so the same TDZ / import-order mistakes surface.
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { JSDOM, VirtualConsole } from 'jsdom'

const HERE = dirname(fileURLToPath(import.meta.url))
const die = (msg) => { console.error(`SMOKE FAIL: ${msg}`); process.exit(1) }

const entry = join(HERE, 'src', 'main.jsx')
if (!existsSync(entry)) die(`no entry point at ${entry}`)

const out = await build({
  entryPoints: [entry],
  bundle: true, write: false, format: 'iife', platform: 'browser',
  loader: { '.jsx': 'jsx', '.css': 'text' },
  jsx: 'automatic',   // same as @vitejs/plugin-react: no `import React` in the source
  define: { 'process.env.NODE_ENV': '"production"' },
  logLevel: 'silent',
}).catch((e) => die(`the app does not even bundle:\n         ${e.message.split('\n')[0]}`))

const code = out.outputFiles[0].text

const errors = []
const vc = new VirtualConsole()
vc.on('jsdomError', (e) => errors.push(e.message || String(e)))

const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div></body></html>',
  { runScripts: 'outside-only', pretendToBeVisual: true,
    url: 'https://localhost:3030/', virtualConsole: vc },
)
const { window } = dom

// Answer the API the way an idle, healthy server would — the point is to
// exercise the NORMAL render path, which is where the white screens happened.
const STUB = {
  '/api/auth': { required: false, authed: true },
  '/api/version': { build: 1, version: 'smoke' },
  '/api/projects': [],
  '/api/engines': { claude: true, codex: true },
  '/api/models': { models: [{ key: 'claude:claude-opus-5', engine: 'claude', model: 'claude-opus-5', label: 'Opus 5' }],
                   engines: { claude: true, codex: true }, default: 'claude:claude-opus-5' },
  '/api/settings': { autoUpdate: true },
  '/api/update/check': { supported: true, behind: 0 },
  '/api/preferences': { text: '' },
  '/api/presets': { presets: [] },
}
window.fetch = (u) => {
  const path = String(u).replace(/^https?:\/\/[^/]+/, '').split('?')[0]
  const body = Object.prototype.hasOwnProperty.call(STUB, path) ? STUB[path] : {}
  return Promise.resolve({ ok: true, status: 200,
                           json: () => Promise.resolve(body),
                           text: () => Promise.resolve(JSON.stringify(body)) })
}
window.matchMedia ||= () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
window.scrollTo ||= () => {}
window.HTMLMediaElement.prototype.play ||= () => Promise.resolve()
window.HTMLMediaElement.prototype.pause ||= () => {}
window.addEventListener('error', (e) => errors.push(e.error?.message || e.message))
window.addEventListener('unhandledrejection', (e) => {
  const m = String(e.reason?.message || e.reason)
  if (!/fetch|network/i.test(m)) errors.push(m)
})

try {
  window.eval(code)
} catch (e) {
  die(`the app threw on load\n         ${e.message}`)
}

await new Promise((r) => setTimeout(r, 2500))

const root = window.document.getElementById('root')
const html = root ? root.innerHTML : ''
if (errors.length) die(`the app threw while mounting\n         ${errors.slice(0, 3).join('\n         ')}`)
if (html.trim().length < 200) die(`#root rendered empty (${html.length} chars) — white screen`)
console.log(`  smoke ok — app mounted, #root has ${html.length} chars`)
process.exit(0)
