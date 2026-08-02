/**
 * The macro panel's logic, not its SVG.
 *
 * Two of these guard bugs that would ship silently: a y-axis anchored at zero
 * flattens DXY≈99.8 into a line at the top of the plot, and a second timer
 * would double the panel's network load without anything visibly changing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MacroContextPanel, {
  deriveMacroStats,
  paddedDomain,
  type MacroSeriesPoint,
} from '../MacroContextPanel'

vi.mock('@/services/api', () => ({
  apiClient: { getMarketCandles: vi.fn() },
}))

import { apiClient } from '@/services/api'

const getCandles = apiClient.getMarketCandles as unknown as ReturnType<typeof vi.fn>

const pts = (closes: number[]): MacroSeriesPoint[] =>
  closes.map((close, i) => ({ time: 1_760_000_000_000 + i * 86_400_000, close }))

beforeEach(() => {
  getCandles.mockReset()
})

describe('deriveMacroStats', () => {
  it('reads the level and the session change off the last two bars', () => {
    const s = deriveMacroStats(pts([100, 101]), 'DXY')
    expect(s.level).toBe(101)
    expect(s.changePct).toBeCloseTo(1.0, 5)
  })

  it('has no change to report from a single bar — null, not NaN', () => {
    const s = deriveMacroStats(pts([100]), 'DXY')
    expect(s.level).toBe(100)
    expect(s.changePct).toBeNull()
  })

  it('is empty-safe', () => {
    expect(deriveMacroStats([], 'VIX')).toEqual({
      level: null, changePct: null, asOf: null, regime: 'neutral',
    })
  })

  it('reads a bid dollar as risk-off and an offered one as risk-on', () => {
    expect(deriveMacroStats(pts([100, 100.5]), 'DXY').regime).toBe('risk-off')
    expect(deriveMacroStats(pts([100, 99.5]), 'DXY').regime).toBe('risk-on')
    expect(deriveMacroStats(pts([100, 100.05]), 'DXY').regime).toBe('neutral')
  })

  it('reads an elevated VIX as risk-off on its level alone', () => {
    expect(deriveMacroStats(pts([26, 26.1]), 'VIX').regime).toBe('risk-off')
    expect(deriveMacroStats(pts([16, 16.05]), 'VIX').regime).toBe('neutral')
  })
})

describe('paddedDomain', () => {
  it('excludes zero for a tightly clustered series', () => {
    const [lo, hi] = paddedDomain(pts([99.6, 99.9, 100.1]))
    expect(lo).toBeGreaterThan(90)
    expect(hi).toBeGreaterThan(100.1)
    expect(lo).toBeLessThan(99.6)
  })

  it('does not collapse to zero height on a flat series', () => {
    const [lo, hi] = paddedDomain(pts([100, 100, 100]))
    expect(hi).toBeGreaterThan(lo)
  })
})

describe('MacroContextPanel', () => {
  it('renders an honest notice when the feed returns no bars', async () => {
    getCandles.mockResolvedValue({ data: { candles: [] } })
    render(<MacroContextPanel refreshNonce={0} />)

    await waitFor(() => {
      expect(screen.getAllByText(/unavailable/i).length).toBeGreaterThan(0)
    })
    expect(screen.getAllByText(/Retry/i).length).toBeGreaterThan(0)
  })

  it('keeps the last good reading visible when a refresh fails', async () => {
    getCandles.mockResolvedValueOnce({
      data: { candles: [{ time: 1_760_000_000, close: 99.5 }, { time: 1_760_086_400, close: 99.8 }] },
    }).mockResolvedValue({ data: { candles: [] } })

    const { rerender } = render(<MacroContextPanel refreshNonce={0} />)
    await waitFor(() => expect(screen.getAllByText(/99\.8/).length).toBeGreaterThan(0))

    rerender(<MacroContextPanel refreshNonce={1} />)
    await waitFor(() => expect(screen.getAllByText(/Refresh failed/i).length).toBeGreaterThan(0))
    // The number the user was reading is still on screen.
    expect(screen.getAllByText(/99\.8/).length).toBeGreaterThan(0)
  })

  it('fetches once per symbol per tick — no second timer', async () => {
    getCandles.mockResolvedValue({
      data: { candles: [{ time: 1_760_000_000, close: 100 }] },
    })

    const { rerender } = render(<MacroContextPanel refreshNonce={0} />)
    await waitFor(() => expect(getCandles).toHaveBeenCalledTimes(2)) // DXY + VIX

    rerender(<MacroContextPanel refreshNonce={1} />)
    await waitFor(() => expect(getCandles).toHaveBeenCalledTimes(4))
  })

  it('asks for daily bars over the requested window', async () => {
    getCandles.mockResolvedValue({ data: { candles: [] } })
    render(<MacroContextPanel refreshNonce={0} lookbackDays={90} />)

    await waitFor(() => expect(getCandles).toHaveBeenCalled())
    expect(getCandles).toHaveBeenCalledWith('DXY', 'D1', 90, expect.anything())
    expect(getCandles).toHaveBeenCalledWith('VIX', 'D1', 90, expect.anything())
  })
})

describe('MacroContextPanel under React strict double-effect', () => {
  it('still loads when the first request is aborted by the remount', async () => {
    // React dev mode runs mount → cleanup → mount. The cleanup aborts request
    // #1; if the second invocation skips because "one is already in flight",
    // nothing ever resolves and the card sits on "Loading…" forever. This is
    // the bug that shipped to the browser before it was caught.
    const { StrictMode } = await import('react')
    getCandles.mockResolvedValue({
      data: { candles: [{ time: 1_760_000_000, close: 99.5 }, { time: 1_760_086_400, close: 99.8 }] },
    })

    render(
      <StrictMode>
        <MacroContextPanel refreshNonce={0} />
      </StrictMode>,
    )

    await waitFor(() => expect(screen.getAllByText(/99\.8/).length).toBeGreaterThan(0))
    expect(screen.queryAllByText(/Loading/i)).toHaveLength(0)
  })
})
