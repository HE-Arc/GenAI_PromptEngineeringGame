import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    // Vite binds localhost only by default, which makes the game unreachable from a phone.
    // `host: true` listens on every interface so players open it over the LAN.
    host: true,
    // BACKEND_URL points at the compose service in Docker, localhost elsewhere.
    proxy: { '/api': { target: process.env.BACKEND_URL ?? 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
