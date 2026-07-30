// High-frequency playback clock OUTSIDE React state.
//
// During playback the time changes ~60×/s; keeping it in useState re-rendered
// the whole app (timeline blocks, inspector, chat…) every frame — the main
// source of choppy playback. Only components that subscribe via useTimeValue()
// (player visuals, playhead, transport label) re-render per frame; everything
// else stays untouched.
import { useSyncExternalStore } from 'react'

const listeners = new Set()
let t = 0

export const timeStore = {
  get: () => t,
  set: (v) => {
    t = Math.max(0, v)
    for (const l of listeners) l()
  },
  subscribe: (l) => {
    listeners.add(l)
    return () => listeners.delete(l)
  },
}

export const useTimeValue = () => useSyncExternalStore(timeStore.subscribe, timeStore.get)
