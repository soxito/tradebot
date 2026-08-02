/**
 * Rendering guards for assistant chat messages.
 *
 * The reported reply listed three Millennium Prize problems and printed every
 * equation as raw LaTeX — `\[ \frac{\partial \mathbf{u}}{\partial t} ... \]` —
 * because the bubble renders pre-wrapped text with no maths support. The
 * markdown around it fared no better: `**bold**` showed its asterisks and
 * numbered steps ran together as one block of prose.
 *
 * These tests run the real renderer over that exact reply.
 */

import React from 'react'
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'

import { renderMarkdown } from '../renderMarkdown'

/** Render to a container and return its text, as the user would read it. */
function textOf(markdown: string): string {
  const { container } = render(<div>{renderMarkdown(markdown)}</div>)
  return container.textContent ?? ''
}

const REPORTED_REPLY = `Sir, the notion of "most difficult" is subjective, but the equations that sit at the heart of the world's most famous unsolved problems are generally regarded as the toughest.

1. **Navier-Stokes existence and smoothness (fluid dynamics)**
   \\[
   \\frac{\\partial \\mathbf{u}}{\\partial t}+(\\mathbf{u}\\!\\cdot\\!\\nabla)\\mathbf{u}
   = -\\nabla p + \\nu\\,\\Delta\\mathbf{u} + \\mathbf{f},
   \\qquad \\nabla\\!\\cdot\\!\\mathbf{u}=0
   \\]
   Proving that smooth solutions exist for all time in three dimensions is a Clay Millennium problem.

2. **Riemann hypothesis**
   \\[
   \\zeta(s)=\\sum_{n=1}^{\\infty}\\frac{1}{n^{s}}=
   \\prod_{p\\ \\text{prime}}\\frac{1}{1-p^{-s}},\\qquad s\\in\\mathbb{C}
   \\]
   The challenge is to prove every non-trivial zero satisfies \\(\\operatorname{Re}(s)=\\tfrac12\\).

Each would earn a $1 million prize.`

describe('the reported reply', () => {
  it('shows no LaTeX source anywhere on screen', () => {
    const text = textOf(REPORTED_REPLY)
    for (const artefact of [
      '\\frac', '\\partial', '\\mathbf', '\\nabla', '\\qquad', '\\sum',
      '\\prod', '\\zeta', '\\infty', '\\mathbb', '\\operatorname', '\\tfrac',
      '\\[', '\\]', '\\(', '\\)',
    ]) {
      expect(text).not.toContain(artefact)
    }
  })

  it('shows the equations as readable symbols instead', () => {
    const text = textOf(REPORTED_REPLY)
    expect(text).toContain('∂')
    expect(text).toContain('∇')
    expect(text).toContain('Δu')
    expect(text).toContain('ζ(s)')
    expect(text).toContain('∑')
    expect(text).toContain('ℂ')
    expect(text).toContain('½')
  })

  it('never shows raw bold asterisks', () => {
    expect(textOf(REPORTED_REPLY)).not.toContain('**')
  })

  it('keeps the $1 million prize as text, not maths', () => {
    expect(textOf(REPORTED_REPLY)).toContain('$1 million prize')
  })

  it('gives each display equation its own scrollable block', () => {
    const { container } = render(<div>{renderMarkdown(REPORTED_REPLY)}</div>)
    const blocks = container.querySelectorAll('.overflow-x-auto')
    expect(blocks).toHaveLength(2)
    // A long formula must scroll inside its own block rather than stretching
    // the chat panel sideways.
    expect(blocks[0].className).toContain('overflow-x-auto')
  })
})

describe('markdown structure', () => {
  it('renders **bold** as an element, not asterisks', () => {
    const { container } = render(<div>{renderMarkdown('the **key term** here')}</div>)
    expect(container.querySelector('strong')?.textContent).toBe('key term')
    expect(container.textContent).not.toContain('*')
  })

  it('renders ## headings without the hashes', () => {
    const text = textOf('## Section Title\nbody')
    expect(text).toContain('Section Title')
    expect(text).not.toContain('#')
  })

  it('renders numbered steps as separate rows', () => {
    const { container } = render(
      <div>{renderMarkdown('1. First step\n2. Second step\n3. Third step')}</div>
    )
    expect(container.textContent).toContain('First step')
    expect(container.textContent).toContain('Third step')
    // Each step is its own flex row, so they stack rather than running together.
    expect(container.querySelectorAll('.flex').length).toBeGreaterThanOrEqual(3)
  })

  it('renders `inline code` as a code element', () => {
    const { container } = render(<div>{renderMarkdown('call `price_lookup` now')}</div>)
    expect(container.querySelector('code')?.textContent).toBe('price_lookup')
    expect(container.textContent).not.toContain('`')
  })

  it('still renders bullets and warnings as before', () => {
    const { container } = render(
      <div>{renderMarkdown('  - first bullet\n  - second bullet')}</div>
    )
    expect(container.textContent).toContain('first bullet')
    expect(render(<div>{renderMarkdown('⚠️ risk warning')}</div>).container.textContent)
      .toContain('risk warning')
  })

  it('preserves ordinary trading replies unchanged', () => {
    const plain = 'BTCUSDT is at 64,120, up 2.1% on the day.'
    expect(textOf(plain)).toBe(plain)
  })

  it('does not throw on empty or malformed input', () => {
    for (const bad of ['', '\\[', '**unclosed', '`unclosed', '\\frac{1']) {
      expect(() => render(<div>{renderMarkdown(bad)}</div>)).not.toThrow()
    }
  })
})

describe('a worked derivation', () => {
  const DERIVATION = [
    'Let $y = x^x$.',
    '',
    '1. Take logs: \\(\\ln(y) = x \\ln(x)\\)',
    '2. Differentiate: \\(\\frac{1}{y}\\frac{dy}{dx} = \\ln(x) + 1\\)',
    '',
    'Therefore \\[\\frac{dy}{dx} = x^x(\\ln(x)+1)\\]',
  ].join('\n')

  it('reads as maths, with the steps numbered', () => {
    const text = textOf(DERIVATION)
    expect(text).not.toContain('\\')
    expect(text).toContain('ln(y) = x ln(x)')
    expect(text).toContain('dy/dx = xˣ(ln(x)+1)')
  })
})
