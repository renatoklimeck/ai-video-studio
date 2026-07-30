import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5195,
    proxy: {
      '/api': 'http://127.0.0.1:3030',
      '/file': 'http://127.0.0.1:3030',
    },
  },
})
