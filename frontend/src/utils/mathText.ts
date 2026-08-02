/**
 * LaTeX → readable Unicode text.
 *
 * The chat bubble renders plain pre-wrapped text with a little inline markdown;
 * it has no maths typesetter. So when a model answered a maths question it
 * emitted `\[ \frac{\partial \mathbf{u}}{\partial t} \]` and the user saw
 * exactly that, backslashes and all — unreadable.
 *
 * Pulling in KaTeX would render it properly, but it is a heavy dependency plus
 * a stylesheet for what is a small share of messages, and it would still leave
 * the many near-miss forms models emit (`$...$`, bare `\nabla`, stray `\!`)
 * looking broken. Converting to Unicode handles every one of those, degrades
 * gracefully on anything unrecognised, and costs nothing at load time.
 *
 * This is deliberately not a LaTeX parser. It is a best-effort transform whose
 * only contract is: the output must be more readable than the input, and it
 * must never throw on malformed input.
 */

const SYMBOLS: Record<string, string> = {
  // Greek — lower case
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε',
  zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ',
  lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ',
  tau: 'τ', upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  // Greek — upper case
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
  Sigma: 'Σ', Upsilon: 'Υ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  // Operators and relations
  partial: '∂', nabla: '∇', infty: '∞', sum: '∑', prod: '∏', int: '∫',
  oint: '∮', pm: '±', mp: '∓', times: '×', div: '÷', cdot: '·', ast: '∗',
  leq: '≤', le: '≤', geq: '≥', ge: '≥', neq: '≠', ne: '≠', approx: '≈',
  equiv: '≡', sim: '∼', simeq: '≃', cong: '≅', propto: '∝',
  in: '∈', notin: '∉', ni: '∋', subset: '⊂', subseteq: '⊆',
  supset: '⊃', supseteq: '⊇', cup: '∪', cap: '∩', emptyset: '∅', varnothing: '∅',
  forall: '∀', exists: '∃', nexists: '∄', neg: '¬', land: '∧', lor: '∨',
  to: '→', rightarrow: '→', Rightarrow: '⇒', leftarrow: '←', Leftarrow: '⇐',
  leftrightarrow: '↔', Leftrightarrow: '⇔', mapsto: '↦', implies: '⇒', iff: '⇔',
  ldots: '…', dots: '…', cdots: '⋯', vdots: '⋮', ddots: '⋱',
  angle: '∠', perp: '⊥', parallel: '∥', degree: '°', prime: '′',
  aleph: 'ℵ', hbar: 'ℏ', ell: 'ℓ', Re: 'Re', Im: 'Im',
  circ: '∘', bullet: '•', star: '⋆', dagger: '†', oplus: '⊕', otimes: '⊗',
}

// Blackboard-bold sets: \mathbb{R} → ℝ
const BLACKBOARD: Record<string, string> = {
  A: '𝔸', C: 'ℂ', D: '𝔻', F: '𝔽', G: '𝔾', H: 'ℍ', K: '𝕂', N: 'ℕ',
  P: 'ℙ', Q: 'ℚ', R: 'ℝ', S: '𝕊', T: '𝕋', Z: 'ℤ',
}

const SUPERSCRIPT: Record<string, string> = {
  '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶',
  '7': '⁷', '8': '⁸', '9': '⁹', '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽',
  ')': '⁾', n: 'ⁿ', i: 'ⁱ', a: 'ᵃ', b: 'ᵇ', c: 'ᶜ', d: 'ᵈ', e: 'ᵉ',
  k: 'ᵏ', m: 'ᵐ', p: 'ᵖ', s: 'ˢ', t: 'ᵗ', x: 'ˣ', y: 'ʸ',
}

const SUBSCRIPT: Record<string, string> = {
  '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', '5': '₅', '6': '₆',
  '7': '₇', '8': '₈', '9': '₉', '+': '₊', '-': '₋', '=': '₌', '(': '₍',
  ')': '₎', a: 'ₐ', e: 'ₑ', h: 'ₕ', i: 'ᵢ', j: 'ⱼ', k: 'ₖ', l: 'ₗ',
  m: 'ₘ', n: 'ₙ', o: 'ₒ', p: 'ₚ', r: 'ᵣ', s: 'ₛ', t: 'ₜ', u: 'ᵤ',
  v: 'ᵥ', x: 'ₓ',
}

const VULGAR_FRACTIONS: Record<string, string> = {
  '1/2': '½', '1/3': '⅓', '2/3': '⅔', '1/4': '¼', '3/4': '¾',
  '1/5': '⅕', '1/8': '⅛', '3/8': '⅜', '5/8': '⅝', '7/8': '⅞',
}

/** Map every char through a table, or return null if any char is unmappable. */
function mapAll(text: string, table: Record<string, string>): string | null {
  let out = ''
  for (const ch of text) {
    const mapped = table[ch]
    if (mapped === undefined) return null
    out += mapped
  }
  return out
}

/**
 * Read a braced group starting at `open` (the index of `{`).
 * Returns the inner text and the index just past the closing brace.
 * Nested braces are respected; an unbalanced group returns null so the caller
 * can leave the original text alone rather than mangling it.
 */
function readGroup(src: string, open: number): { body: string; end: number } | null {
  if (src[open] !== '{') return null
  let depth = 0
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}') {
      depth--
      if (depth === 0) return { body: src.slice(open + 1, i), end: i + 1 }
    }
  }
  return null
}

/**
 * Replace every `\name{...}` occurrence, giving the handler the group body.
 * Brace-aware, so `\frac{\frac{1}{2}}{3}` does not terminate at the first `}`.
 */
function replaceCommand(
  src: string,
  name: string,
  arity: 1 | 2,
  render: (a: string, b: string) => string,
): string {
  const marker = `\\${name}`
  let out = ''
  let i = 0
  while (i < src.length) {
    const at = src.indexOf(marker, i)
    if (at === -1) { out += src.slice(i); break }
    // Guard against \nu matching inside \nabla: the next char must not extend
    // the command name.
    const after = src[at + marker.length]
    if (after !== undefined && /[a-zA-Z]/.test(after)) {
      out += src.slice(i, at + marker.length)
      i = at + marker.length
      continue
    }
    let cursor = at + marker.length
    while (src[cursor] === ' ') cursor++
    const first = readGroup(src, cursor)
    if (!first) { out += src.slice(i, at + marker.length); i = at + marker.length; continue }
    let second: { body: string; end: number } | null = null
    if (arity === 2) {
      let c2 = first.end
      while (src[c2] === ' ') c2++
      second = readGroup(src, c2)
      if (!second) { out += src.slice(i, at + marker.length); i = at + marker.length; continue }
    }
    out += src.slice(i, at)
    out += render(first.body, second ? second.body : '')
    i = second ? second.end : first.end
  }
  return out
}

/**
 * Replace `^{...}` / `_{...}` with Unicode super/subscripts.
 *
 * Falls back to `^(...)` when the body has a character with no Unicode
 * equivalent — `x^{n+1}` is readable, whereas dropping to a bare `xn+1` would
 * silently change what the expression says.
 */
function replaceScript(src: string, sigil: '^' | '_', table: Record<string, string>): string {
  let out = ''
  let i = 0
  while (i < src.length) {
    const at = src.indexOf(`${sigil}{`, i)
    if (at === -1) { out += src.slice(i); break }
    const group = readGroup(src, at + 1)
    if (!group) { out += src.slice(i, at + 1); i = at + 1; continue }
    const body = convertMath(group.body).trim()
    out += src.slice(i, at) + (mapAll(body, table) ?? `${sigil}(${body})`)
    i = group.end
  }
  return out
}

/** True when an expression is simple enough to sit in a fraction unbracketed. */
function isAtomic(text: string): boolean {
  return /^[A-Za-z0-9∂π]{1,3}$/.test(text.trim())
}

function renderFraction(numerator: string, denominator: string): string {
  const a = convertMath(numerator).trim()
  const b = convertMath(denominator).trim()
  const vulgar = VULGAR_FRACTIONS[`${a}/${b}`]
  if (vulgar) return vulgar
  const left = isAtomic(a) ? a : `(${a})`
  const right = isAtomic(b) ? b : `(${b})`
  return `${left}/${right}`
}

/**
 * Convert the body of a maths expression to Unicode.
 * Order matters: structural commands (fractions, roots) resolve before
 * single-token symbols, and spacing junk is stripped last.
 */
export function convertMath(input: string): string {
  let s = input

  // Structural commands, innermost-first via recursion in the renderers.
  // \frac12 is the brace-free shorthand for one-digit arguments.
  s = s.replace(/\\[dt]?frac(\d)(\d)/g, (_m, a: string, b: string) => renderFraction(a, b))
  s = replaceCommand(s, 'frac', 2, renderFraction)
  s = replaceCommand(s, 'dfrac', 2, renderFraction)
  s = replaceCommand(s, 'tfrac', 2, renderFraction)
  s = replaceCommand(s, 'sqrt', 1, (a) => {
    const body = convertMath(a).trim()
    return isAtomic(body) ? `√${body}` : `√(${body})`
  })
  s = replaceCommand(s, 'mathbb', 1, (a) => BLACKBOARD[a.trim()] ?? a)
  // \stackrel{?}{=} is the "does this equal" of an open conjecture.
  s = replaceCommand(s, 'stackrel', 2, (a, b) => {
    const over = convertMath(a).trim()
    const base = convertMath(b).trim()
    if (over === '?' && base === '=') return '≟'
    return over ? `${base}[${over}]` : base
  })

  // Single-token symbols run BEFORE the styling wrappers are unwrapped.
  // The other order breaks `\Delta\mathbf{u}`: dropping \mathbf first leaves
  // `\Deltau`, and the trailing letter then defeats the \Delta lookahead.
  // Longest names first so \subseteq beats \subset.
  const names = Object.keys(SYMBOLS).sort((a, b) => b.length - a.length)
  s = s.replace(
    new RegExp(`\\\\(${names.join('|')})(?![a-zA-Z])`, 'g'),
    (_m, name: string) => SYMBOLS[name],
  )
  // Standard function names keep their spelling, minus the backslash.
  s = s.replace(
    /\\(ln|log|exp|sin|cos|tan|sec|csc|cot|arcsin|arccos|arctan|sinh|cosh|tanh|lim|limsup|liminf|max|min|sup|inf|det|dim|gcd|arg|deg|ker|hom|Pr|mod)(?![a-zA-Z])/g,
    '$1',
  )

  // Styling wrappers carry no meaning once typesetting is gone.
  for (const cmd of ['mathbf', 'mathrm', 'mathit', 'mathcal', 'mathsf', 'boldsymbol', 'vec', 'bar', 'hat', 'text', 'textbf', 'textrm', 'operatorname', 'mbox']) {
    s = replaceCommand(s, cmd, 1, (a) => convertMath(a))
  }

  // Super/subscripts. The bare single-char form MUST run before the braced
  // one: the braced pass falls back to `^(…)` when a character has no Unicode
  // superscript, and running the bare pass afterwards would superscript that
  // very `(`, turning `^(∞)` into `⁽∞)`. Neither bare pattern can match `{`,
  // so this order is safe for both.
  s = s.replace(/\^([A-Za-z0-9+\-])/g, (whole, ch: string) => SUPERSCRIPT[ch] ?? whole)
  s = s.replace(/_([A-Za-z0-9+\-])/g, (whole, ch: string) => SUBSCRIPT[ch] ?? whole)
  s = replaceScript(s, '^', SUPERSCRIPT)
  s = replaceScript(s, '_', SUBSCRIPT)
  // Anything still braced is a bare grouping brace, meaningless in plain text.
  s = s.replace(/\{([^{}]*)\}/g, '$1')

  // Spacing and sizing directives — pure typesetting, no meaning in plain text.
  s = s.replace(/\\\\/g, '\n')          // row break inside an environment
  // \! is a NEGATIVE thin space and \, \; \: are thin spaces: the author wrote
  // them to pull terms together, so rendering them as a full space says the
  // opposite of what the source meant. Only \quad/\qquad are real gaps.
  s = s.replace(/\\[!,;:]/g, '')
  s = s.replace(/\\(?:qquad|quad|\s)/g, ' ')
  s = s.replace(/\\(?:left|right|big|Big|bigg|Bigg|displaystyle|limits|nolimits)\b/g, '')
  s = s.replace(/&/g, ' ')              // alignment marker

  // Tidy the spacing the substitutions leave behind. Unwrapping `\mathbf{u}`
  // leaves `∂ u` where the source read `\partial \mathbf{u}`; an operator
  // binds to the term it applies to, so close that gap.
  s = s.replace(/([∂∇√∑∏∫ΔδπΓΛΩ])[ \t]+(?=[A-Za-z(])/g, '$1')
  s = s.replace(/[ \t]{2,}/g, ' ')
  s = s.replace(/[ \t]+\n/g, '\n')
  return s.trim()
}

/** A single extracted maths span and where it came from. */
export interface MathSegment {
  kind: 'text' | 'inline-math' | 'display-math'
  content: string
}

/**
 * Is a `$…$` span really maths, or two currency amounts in one sentence?
 *
 * This is a trading assistant: "$5" and "$2,400" appear constantly, and a
 * naive `$…$` rule turns "costs $5 and earns $10" into a maths span, deleting
 * the words between the two amounts. `$` is only read as a maths delimiter
 * when the body actually carries maths notation — a LaTeX command or a
 * super/subscript. Missing a plain `$x$` is a cosmetic loss; eating a sentence
 * is a real one.
 */
function looksLikeMathDollar(body: string): boolean {
  if (/^\s|\s$/.test(body)) return false            // "$5 and earns $" — padded
  if (/^[\d,.]+\s/.test(body)) return false          // starts as an amount
  return /\\[a-zA-Z]|[\^_]/.test(body)               // has real maths notation
}

// Display first (`\[…\]`, `$$…$$`, environments), then inline (`\(…\)`, `$…$`).
// `$…$` is last, must not span a line, and is additionally filtered above.
const MATH_PATTERNS: Array<{
  re: RegExp
  kind: MathSegment['kind']
  valid?: (body: string) => boolean
}> = [
  { re: /\\\[([\s\S]*?)\\\]/g, kind: 'display-math' },
  { re: /\$\$([\s\S]*?)\$\$/g, kind: 'display-math' },
  { re: /\\begin\{(?:equation|align|aligned|gather|displaymath)\*?\}([\s\S]*?)\\end\{(?:equation|align|aligned|gather|displaymath)\*?\}/g, kind: 'display-math' },
  { re: /\\\(([\s\S]*?)\\\)/g, kind: 'inline-math' },
  { re: /\$([^$\n]+?)\$/g, kind: 'inline-math', valid: looksLikeMathDollar },
]

/**
 * Split text into plain and maths segments, converting the maths to Unicode.
 * Text with no maths returns a single 'text' segment, so callers can cheaply
 * detect the common case.
 */
export function splitMath(text: string): MathSegment[] {
  if (!text) return [{ kind: 'text', content: '' }]

  type Hit = { start: number; end: number; kind: MathSegment['kind']; body: string }
  const hits: Hit[] = []
  for (const { re, kind, valid } of MATH_PATTERNS) {
    re.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(text)) !== null) {
      if (valid && !valid(m[1])) continue
      // Skip anything overlapping a hit an earlier (higher-priority) pattern
      // already claimed — `$$x$$` must not also match the `$…$` rule.
      const overlaps = hits.some(h => m!.index < h.end && h.start < m!.index + m![0].length)
      if (!overlaps) {
        hits.push({ start: m.index, end: m.index + m[0].length, kind, body: m[1] })
      }
    }
  }
  if (!hits.length) return [{ kind: 'text', content: text }]

  hits.sort((a, b) => a.start - b.start)
  const out: MathSegment[] = []
  let cursor = 0
  for (const hit of hits) {
    if (hit.start > cursor) out.push({ kind: 'text', content: text.slice(cursor, hit.start) })
    const converted = convertMath(hit.body)
    if (converted) out.push({ kind: hit.kind, content: converted })
    cursor = hit.end
  }
  if (cursor < text.length) out.push({ kind: 'text', content: text.slice(cursor) })
  return out
}

/** True if the text contains anything worth running through the converter. */
export function hasMath(text: string): boolean {
  return /\\[[(]|\$\$|\\begin\{|\\(?:frac|sum|prod|int|partial|nabla|infty|sqrt|mathbb|zeta|alpha|beta|delta|lambda|sigma|omega|theta|cdot|leq|geq|neq|approx|in|to)\b/.test(text)
}

/**
 * Flatten maths to Unicode without segmenting — for speech synthesis and any
 * other consumer that just wants readable characters in a single string.
 */
export function flattenMath(text: string): string {
  return splitMath(text).map(s => s.content).join(' ').replace(/[ \t]{2,}/g, ' ').trim()
}
