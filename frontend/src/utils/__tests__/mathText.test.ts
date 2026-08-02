/**
 * Guards on LaTeX → Unicode conversion in chat replies.
 *
 * JARVIS answered a maths question with `\[ \frac{\partial \mathbf{u}}{\partial
 * t} \]` and the chat panel — which has no maths typesetter — printed it
 * verbatim, backslashes and all. These tests pin the conversion, and in
 * particular the three ordering bugs that made the first version worse than
 * useless: `\Delta\mathbf{u}` collapsing to `\Deltau`, `^(∞)` fallbacks being
 * re-superscripted into `⁽∞)`, and brace-free `\tfrac12` being ignored.
 */

import { describe, it, expect } from 'vitest'

import { convertMath, splitMath, flattenMath, hasMath } from '../mathText'

describe('convertMath', () => {
  it('converts fractions, using vulgar glyphs where one exists', () => {
    expect(convertMath('\\frac{1}{2}')).toBe('½')
    expect(convertMath('\\frac{dy}{dx}')).toBe('dy/dx')
    expect(convertMath('\\frac{a+b}{c}')).toBe('(a+b)/c')
  })

  it('handles the brace-free \\tfrac12 shorthand', () => {
    expect(convertMath('\\tfrac12')).toBe('½')
    expect(convertMath('\\frac34')).toBe('¾')
  })

  it('keeps a symbol intact when a styling wrapper follows it', () => {
    // \Delta\mathbf{u} used to unwrap to `\Deltau`, where the trailing letter
    // defeated the \Delta lookahead and left the backslash on screen.
    expect(convertMath('\\Delta\\mathbf{u}')).toBe('Δu')
    expect(convertMath('\\nu\\,\\Delta\\mathbf{u}')).toContain('Δu')
  })

  it('does not let a short command name match inside a longer one', () => {
    expect(convertMath('\\int')).toBe('∫')
    expect(convertMath('\\in')).toBe('∈')
    expect(convertMath('\\subseteq')).toBe('⊆')
  })

  it('converts super and subscripts to Unicode', () => {
    expect(convertMath('x^{2}')).toBe('x²')
    expect(convertMath('x^2')).toBe('x²')
    expect(convertMath('x^{n+1}')).toBe('xⁿ⁺¹')
    expect(convertMath('a_{ij}')).toBe('aᵢⱼ')
    expect(convertMath('n^{s}')).toBe('nˢ')
  })

  it('falls back readably when a script has no Unicode form', () => {
    // ∞ has no superscript glyph. The fallback must stay `^(∞)` — an earlier
    // version superscripted the fallback's own bracket into `⁽∞)`.
    const out = convertMath('\\sum_{n=1}^{\\infty}')
    expect(out).toContain('^(∞)')
    expect(out).not.toContain('⁽')
    expect(out).toContain('∑')
  })

  it('renders roots, sets and function names', () => {
    expect(convertMath('\\sqrt{x^2+1}')).toBe('√(x²+1)')
    expect(convertMath('\\sqrt{2}')).toBe('√2')
    expect(convertMath('\\mathbb{R}^3')).toBe('ℝ³')
    expect(convertMath('s\\in\\mathbb{C}')).toBe('s∈ℂ')
    expect(convertMath('\\ln(x)')).toBe('ln(x)')
  })

  it('renders \\stackrel{?}{=} as the "is it equal" glyph', () => {
    expect(convertMath('\\stackrel{?}{=}')).toBe('≟')
  })

  it('strips pure typesetting directives', () => {
    expect(convertMath('a\\qquad b')).toBe('a b')
    expect(convertMath('\\left(x\\right)')).toBe('(x)')
  })

  it('honours thin and negative spaces by tightening, not widening', () => {
    // \! is a NEGATIVE space; rendering it as a gap says the opposite of what
    // the author wrote.
    expect(convertMath('a\\!\\cdot\\!b')).toBe('a·b')
    expect(convertMath('\\nu\\,\\Delta')).toBe('νΔ')
  })

  it('binds an operator to the term it applies to', () => {
    // Unwrapping \mathbf{u} leaves `∂ u`; the operator should not float free.
    expect(convertMath('\\frac{\\partial \\mathbf{u}}{\\partial t}')).toBe('∂u/∂t')
    expect(convertMath('-\\nabla p')).toBe('-∇p')
  })

  it('never throws on malformed input', () => {
    for (const bad of ['\\frac{1', '\\sqrt{', '^{', '{{{', '\\', '\\frac{}{}']) {
      expect(() => convertMath(bad)).not.toThrow()
    }
  })
})

describe('splitMath', () => {
  it('returns a single text segment when there is no maths', () => {
    const segs = splitMath('BTCUSDT is at 64,000 right now.')
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('text')
  })

  it('extracts display maths spanning several lines', () => {
    const segs = splitMath('Here:\n\\[\n\\frac{a}{b}\n\\]\ndone')
    expect(segs.map(s => s.kind)).toEqual(['text', 'display-math', 'text'])
    expect(segs[1].content).toBe('a/b')
  })

  it('extracts inline maths', () => {
    const segs = splitMath('zero at \\(\\operatorname{Re}(s)=\\tfrac12\\).')
    expect(segs.find(s => s.kind === 'inline-math')?.content).toBe('Re(s)=½')
  })

  it('treats $$…$$ as display and $…$ as inline', () => {
    expect(splitMath('$$x^2$$')[0].kind).toBe('display-math')
    expect(splitMath('value $x^2$ here').some(s => s.kind === 'inline-math')).toBe(true)
  })

  it('does not swallow the text between two dollar amounts', () => {
    // "$1 million prize and a place worth $500" must stay one text run —
    // treating the span as maths would delete the sentence between them.
    const segs = splitMath('costs $5 and earns $10 later')
    expect(segs.every(s => s.kind === 'text')).toBe(true)
  })

  it('leaves real currency amounts alone', () => {
    // This is a trading assistant; dollar figures are everywhere. Any of these
    // being read as a maths delimiter would delete the words between them.
    for (const line of [
      'would earn a $1 million prize and a place in history',
      'entry at $64,000 with a stop at $62,500',
      'it cost $5 and returned $12 in profit',
    ]) {
      expect(splitMath(line).every(s => s.kind === 'text')).toBe(true)
      expect(splitMath(line).map(s => s.content).join('')).toBe(line)
    }
  })

  it('still reads $x^2$ as maths when the body really is notation', () => {
    expect(splitMath('take $x^2$ here').some(s => s.kind === 'inline-math')).toBe(true)
  })

  it('does not double-match $$ as two inline $ spans', () => {
    const segs = splitMath('$$a+b$$')
    expect(segs.filter(s => s.kind !== 'text')).toHaveLength(1)
  })

  it('handles equation environments', () => {
    const segs = splitMath('\\begin{equation}x^2\\end{equation}')
    expect(segs.find(s => s.kind === 'display-math')?.content).toBe('x²')
  })
})

describe('the reported Navier-Stokes reply', () => {
  const REPLY = [
    '1. **Navier-Stokes existence and smoothness**',
    '   \\[',
    '   \\frac{\\partial \\mathbf{u}}{\\partial t}+(\\mathbf{u}\\!\\cdot\\!\\nabla)\\mathbf{u}',
    '   = -\\nabla p + \\nu\\,\\Delta\\mathbf{u} + \\mathbf{f},',
    '   \\qquad \\nabla\\!\\cdot\\!\\mathbf{u}=0',
    '   \\]',
  ].join('\n')

  it('leaves no LaTeX artefacts on screen', () => {
    const rendered = splitMath(REPLY).map(s => s.content).join(' ')
    for (const artefact of ['\\frac', '\\partial', '\\mathbf', '\\nabla', '\\qquad', '\\[', '\\]']) {
      expect(rendered).not.toContain(artefact)
    }
  })

  it('produces the readable equation', () => {
    const math = splitMath(REPLY).find(s => s.kind === 'display-math')!
    expect(math.content).toContain('∂')
    expect(math.content).toContain('∇')
    expect(math.content).toContain('Δu')
    expect(math.content).not.toContain('\\')
  })
})

describe('flattenMath (used for speech)', () => {
  it('gives the voice symbols rather than backslash commands', () => {
    const spoken = flattenMath('The rate is \\(\\frac{\\partial u}{\\partial t}\\).')
    expect(spoken).not.toContain('\\')
    expect(spoken).toContain('∂')
  })
})

describe('hasMath', () => {
  it('detects LaTeX and ignores ordinary chat', () => {
    expect(hasMath('\\[ x \\]')).toBe(true)
    expect(hasMath('\\frac{1}{2}')).toBe(true)
    expect(hasMath('Gold is at 2,400 and BTC is up 3%.')).toBe(false)
  })
})
