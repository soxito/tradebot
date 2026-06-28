import { Component, ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

interface Props {
  children: ReactNode
  /** Optional label shown in the fallback. */
  label?: string
}

interface State {
  hasError: boolean
  resetKey: number
}

/**
 * Isolates a subtree (e.g. the lightweight-charts canvas) so any runtime throw
 * shows a recoverable fallback instead of blanking the whole page. The user can
 * remount the subtree with the "Reload" button.
 */
export class ChartErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, resetKey: 0 }

  static getDerivedStateFromError(): Partial<State> {
    return { hasError: true }
  }

  componentDidCatch(error: unknown) {
    // Keep a console trace for debugging without crashing the app.
    // eslint-disable-next-line no-console
    console.warn('[ChartErrorBoundary] recovered from chart error:', error)
  }

  private handleReset = () => {
    this.setState(s => ({ hasError: false, resetKey: s.resetKey + 1 }))
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center gap-3 p-8 text-center bg-[#0b0e16] border border-gray-700/50 rounded-xl min-h-[300px]">
          <AlertTriangle className="w-8 h-8 text-amber-400" />
          <div className="text-sm text-gray-300">
            {this.props.label ?? 'The chart hit a rendering error.'}
          </div>
          <button
            onClick={this.handleReset}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyan-600/20 text-cyan-300 border border-cyan-500/30 text-xs font-medium hover:bg-cyan-600/30"
          >
            <RefreshCw className="w-3.5 h-3.5" /> Reload chart
          </button>
        </div>
      )
    }
    // Changing the key on reset forces the subtree to remount cleanly.
    return <div key={this.state.resetKey} className="contents">{this.props.children}</div>
  }
}

export default ChartErrorBoundary
