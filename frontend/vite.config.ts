import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Axum is the only API server: every /api route (REST and WS upgrade)
// goes to :3001. `AXUM_URL` points at the compose service in Docker,
// localhost elsewhere.
export default defineConfig({
  plugins: [react()],
  server: {
    // Vite binds localhost only by default, which makes the game unreachable from a phone.
    // `host: true` listens on every interface so players open it over the LAN.
    host: true,
    proxy: {
      '/api': {
        target: process.env.AXUM_URL ?? 'http://127.0.0.1:3001',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
