/**
 * The finding card's body is readable.
 *
 * A prediction card carries the whole trade plan — both entries with their
 * stops and targets, the reconciliation, the sources — in `body`. Clamped to
 * two lines with no way to open it, the numbers a person would act on are
 * exactly the ones they cannot read.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// The shared setup mocks a fixed handful of icons; this page pulls a dozen.
// Any name resolves to the same inert stub — the card's behaviour is the
// subject here, not its glyphs.
vi.mock('lucide-react', () => {
  const Stub = () => null
  return new Proxy({ __esModule: true } as Record<string, unknown>, {
    get: (target, prop) => {
      if (prop === '__esModule') return true
      // `then` must stay undefined: the module namespace is awaited during
      // import, and a callable `then` makes it look like a pending thenable.
      if (prop === 'then' || typeof prop === 'symbol') return undefined
      return (target[prop as string] ??= Stub)
    },
  })
})

import FindingCard from '@/components/research/FindingCard'
import type { ResearchFinding } from '@/hooks/useResearchFeed'

const PLAN = [
  'Entries:',
  '- primary: SELL entry 4.45 | SL 4.672 | TP 4.417 | 0.15R | conf 65%',
  '  trigger: Price retests 4.45',
  '- secondary: SELL entry 4.55 | SL 4.672 | TP 4.417 | 1.09R | conf 55%',
  '',
  'The single Telegram signal is bearish, corroborated by the price structure.',
].join('\n')

const finding = (over: Partial<ResearchFinding> = {}): ResearchFinding =>
  ({
    id: 1,
    kind: 'prediction',
    symbol: 'INJUSDT',
    headline: 'INJUSDT: bearish (65%)',
    body: PLAN,
    source: 'signal-research',
    source_url: 'https://example.com/a',
    confidence: 0.65,
    speculative: false,
    provider_used: 'stub',
    published_at: null,
    decay_at: new Date(Date.now() + 3_600_000).toISOString(),
    created_at: new Date().toISOString(),
    ...over,
  }) as ResearchFinding

const bodyNode = () => screen.getByText(/- primary: SELL entry 4\.45/)

describe('FindingCard', () => {
  it('clamps a long plan but offers to open it', () => {
    render(<FindingCard finding={finding()} />)

    expect(bodyNode().className).toContain('line-clamp-2')
    expect(screen.getByRole('button', { expanded: false })).toBeTruthy()
    expect(screen.getByText('read full research')).toBeTruthy()
  })

  it('shows the whole plan once clicked', () => {
    render(<FindingCard finding={finding()} />)

    fireEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByRole('button', { expanded: true })).toBeTruthy()
    expect(screen.getByText('show less')).toBeTruthy()
    // Newlines preserved, so both entries read as separate lines.
    const shown = bodyNode()
    expect(shown.className).toContain('whitespace-pre-wrap')
    expect(shown.className).not.toContain('line-clamp-2')
    expect(shown.textContent).toContain('SL 4.672')
    expect(shown.textContent).toContain('TP 4.417')
    expect(shown.textContent).toContain('- secondary: SELL entry 4.55')
  })

  it('collapses again on a second click', () => {
    render(<FindingCard finding={finding()} />)

    fireEvent.click(screen.getByRole('button', { expanded: false }))
    fireEvent.click(screen.getByRole('button', { expanded: true }))

    expect(bodyNode().className).toContain('line-clamp-2')
    expect(screen.getByText('read full research')).toBeTruthy()
  })

  it('offers no toggle for a one-line body', () => {
    render(<FindingCard finding={finding({ body: 'Fear & Greed is 30.' })} />)

    expect(screen.queryByText('read full research')).toBeNull()
  })

  it('renders a card with no body at all', () => {
    render(<FindingCard finding={finding({ body: '' })} />)

    expect(screen.getByText('INJUSDT: bearish (65%)')).toBeTruthy()
    expect(screen.queryByText('read full research')).toBeNull()
  })
})
