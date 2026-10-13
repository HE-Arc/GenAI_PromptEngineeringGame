import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
// Order matters: tokens and the element base, then the shared instruments, then the screens
// that lay them out. A later sheet may only place a shape, never recolour one.
import './index.css'
import './console.css'
import './pages.css'
import './polish.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
