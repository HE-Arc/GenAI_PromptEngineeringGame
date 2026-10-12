import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import type { Socket } from 'node:net'

// A peer that hung up: a phone that slept, a tab reloaded mid-stream. Writing to it fails with one
// of these, and there is nothing to report — the client reconnects on its own.
const HUNG_UP = new Set(['EPIPE', 'ECONNRESET', 'ECONNABORTED'])

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
        configure: (proxy) => {
          // The same hang-up seen from the other side: a WebSocket whose browser vanished (a
          // phone locked mid-round) resets, and Vite logs it as `ws proxy error: ECONNRESET`.
          // Vite adds that listener right after `configure` returns; the microtask wraps it so
          // HTTP errors and real WebSocket failures are still logged.
          queueMicrotask(() => {
            const viteLog = proxy.listeners('error') as ((...a: unknown[]) => void)[]
            proxy.removeAllListeners('error')
            proxy.on('error', (err: NodeJS.ErrnoException, req, res) => {
              const isWs = !(res && 'req' in res)
              if (isWs && err.code && HUNG_UP.has(err.code)) {
                ;(res as Socket | undefined)?.destroy?.()
                return
              }
              for (const l of viteLog) l.call(proxy, err, req, res)
            })
          })
          proxy.on('proxyReqWs', (proxyReq, _req, socket: Socket) => {
            // Vite attaches its own logging listener to the browser socket right after this one,
            // in the same emit; the microtask runs once it is there. Its line is kept for real
            // errors and dropped for a peer that hung up, and either way the upstream half is
            // closed — Vite's listener only logs, so a dead browser socket left Axum's side open.
            queueMicrotask(() => {
              const viteLog = socket
                .listeners('error')
                .filter((l) => String(l).includes('ws proxy socket error'))
              for (const l of viteLog) socket.off('error', l as (err: Error) => void)
              socket.on('error', (err: NodeJS.ErrnoException) => {
                proxyReq.destroy()
                if (err.code && HUNG_UP.has(err.code)) return
                for (const l of viteLog) (l as (err: Error) => void).call(socket, err)
              })
            })
          })
        },
      },
    },
  },
})
