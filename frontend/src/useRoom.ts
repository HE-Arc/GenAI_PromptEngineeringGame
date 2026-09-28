import { useEffect, useState } from 'react'
import type { State } from './api'

export function useRoom(code: string | null) {
  const [state, setState] = useState<State | null>(null)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!code) return
    const es = new EventSource(`/api/rooms/${code}/events`)
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    es.onmessage = (e) => setState(JSON.parse(e.data) as State)
    return () => es.close()
  }, [code])

  return { state, connected }
}
