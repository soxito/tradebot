// Telling JARVIS's own voice apart from the user's, from the transcript alone.
//
// Speakers leak the reply back into the microphone, and the recogniser writes
// it down — imperfectly: a word dropped, "sir" heard as "sure", two sentences
// run together, and often the tail of his reply glued to the front of what the
// user started saying over it. The old test only caught verbatim copies, so a
// slightly misheard echo passed as a command and JARVIS answered himself.
//
// Two operations, both word-level (never substring — "cat" must not match
// inside "concatenate"):
//   • stripSelfEcho removes the stretches of a transcript that follow JARVIS's
//     own word order, keeping whatever the user said around them.
//   • isSelfEchoText is true when nothing of the user's is left after that.
//
// Keep jarvis-extension/content.js in step: it carries a plain-JS copy.

export function normSpeech(s: string): string {
  return String(s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim()
}

// Filler that carries no meaning of its own: left over after an echo is
// stripped, it is not the user saying anything.
const FILLER = new Set(
  ('a an the and or but so to of in on at for is it its be are was i you he she we they ' +
   'me my your sir yes yeah ok okay uh um oh well just now this that').split(' '),
)

/** Adjacent word pairs of `words`, as "a b" strings. */
function pairs(words: string[]): Set<string> {
  const out = new Set<string>()
  for (let i = 0; i + 1 < words.length; i++) out.add(`${words[i]} ${words[i + 1]}`)
  return out
}

/**
 * `transcript` with every run that reproduces JARVIS's word order removed.
 *
 * A run is two or more consecutive transcript words whose adjacent pairs all
 * occur, in order, in what JARVIS said. One misheard word inside an echo only
 * splits it into two runs — both still go. Runs of two words are removed only
 * at the edges of the transcript, where echo tails actually land; mid-sentence
 * a user can legitimately repeat a two-word phrase ("the gold") of the reply.
 */
export function stripSelfEcho(transcript: string, lastSpoken: string): string {
  const t = normSpeech(transcript)
  const l = normSpeech(lastSpoken)
  if (!t || !l) return t
  const tw = t.split(' ')
  const known = pairs(l.split(' '))
  const drop = new Array<boolean>(tw.length).fill(false)
  let i = 0
  while (i < tw.length - 1) {
    let j = i
    while (j + 1 < tw.length && known.has(`${tw[j]} ${tw[j + 1]}`)) j++
    const len = j - i + 1
    if (len >= 3 || (len === 2 && (i === 0 || j === tw.length - 1))) {
      for (let k = i; k <= j; k++) drop[k] = true
    }
    i = Math.max(j, i + 1)
  }
  return tw.filter((_, k) => !drop[k]).join(' ')
}

/** Words that actually say something (not filler, not a lone letter). */
export function contentWords(s: string): string[] {
  return normSpeech(s).split(' ').filter(w => w.length > 1 && !FILLER.has(w))
}

/**
 * True when `transcript` is JARVIS's own reply coming back through the mic.
 *
 * Conservative where it matters: a transcript is only echo when *nothing the
 * user said* survives — a genuine interruption glued onto an echo tail keeps
 * the user's words (use stripSelfEcho to recover them).
 */
export function isSelfEchoText(transcript: string, lastSpoken: string): boolean {
  if (!lastSpoken) return false
  const t = normSpeech(transcript)
  const l = normSpeech(lastSpoken)
  if (!t || !l) return false
  // Verbatim, including a one- or two-word fragment ("yes sir").
  if (` ${l} `.includes(` ${t} `)) return true
  const left = contentWords(stripSelfEcho(t, l))
  if (left.length === 0) return true
  // Order-free overlap for a badly garbled echo: nearly every meaningful word
  // is one JARVIS just used, and there are enough of them to be sure.
  const tc = contentWords(t)
  if (tc.length < 3) return false
  const vocab = new Set(contentWords(l))
  const hit = tc.filter(w => vocab.has(w)).length
  return hit >= 3 && hit / tc.length >= 0.75
}

/**
 * Join letters the recogniser spelled out: "x a u u s d" → "XAUUSD".
 * A ticker said letter by letter arrives as one-letter words no symbol parser
 * recognises. Three or more in a row are joined (so "I a" is left alone).
 * Mirrors collapse_spelled_letters in backend/app/api/voice.py.
 */
export function collapseSpelledLetters(text: string): string {
  return (text || '').replace(
    /\b(?:[A-Za-z](?:\. ?| )){2,}[A-Za-z]\b\.?/g,
    m => m.replace(/[. ]/g, '').toUpperCase(),
  )
}
