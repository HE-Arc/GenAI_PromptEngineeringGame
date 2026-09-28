import { useEffect, useState } from 'react'
import type { State } from './api'

export function useAdminRoom(code: string | null) {
  const [state, setState] = useState<State | null>(null)

  useEffect(() => {
    if (!code) return
    const es = new EventSource(`/api/rooms/${code}/events`)
    es.onmessage = (e) => setState(JSON.parse(e.data) as State)
    return () => es.close()
  }, [code])

  return { state }
}
