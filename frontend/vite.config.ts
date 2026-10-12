import react from '@vitejs/plugin-react'
import { defineConfig, type ProxyOptions } from 'vite'

// Strangler proxy table: Django stays the default for every /api route so any
// cut step rolls back by flipping one entry back to Django with no redeploy.
// Each entry pairs a path prefix with the env flag that moves it to Axum;
// adding a later step means appending one line here, not restructuring.
function proxyFor(env: NodeJS.ProcessEnv): Record<string, ProxyOptions> {
  // BACKEND_URL points at the compose service in Docker, localhost elsewhere.
  const django = env.BACKEND_URL ?? 'http://127.0.0.1:8000'
  const axum = env.AXUM_URL ?? 'http://127.0.0.1:3001'
  const rest = { target: django, changeOrigin: true }
  // Vite matches proxy keys in insertion order, so the WS regex must come
  // first: otherwise the generic /api prefix would swallow the WS upgrade
  // and the socket would hit Django instead of Axum.
  if (env.VITE_API_WS === '1') {
    return {
      '^/api/rooms/[^/]+/ws': { target: axum, changeOrigin: true, ws: true },
      '/api': rest,
    }
  }
  return { '/api': { ...rest, ws: true } }
}

export default defineConfig({
  plugins: [react()],
  server: {
    // Vite binds localhost only by default, which makes the game unreachable from a phone.
    // `host: true` listens on every interface so players open it over the LAN.
    host: true,
    proxy: proxyFor(process.env),
  },
})
