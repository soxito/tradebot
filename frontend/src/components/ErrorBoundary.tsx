'use client'
import { Component, ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
  onError?: (error: Error, errorInfo: ErrorInfo) => void
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('[ErrorBoundary]', error, errorInfo)
    this.props.onError?.(error, errorInfo)
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback
      }
      return (
        <div style={{
          position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)',
          padding: '12px 20px', background: 'rgba(239,68,68,0.9)', color: 'white',
          borderRadius: 8, fontSize: 13, fontFamily: 'monospace', zIndex: 9999
        }}>
          ⚠️ JARVIS component error.{' '}
          <button onClick={() => window.location.reload()} style={{marginLeft:8, cursor:'pointer', textDecoration: 'underline'}}>Reload</button>
        </div>
      )
    }
    return this.props.children
  }
}