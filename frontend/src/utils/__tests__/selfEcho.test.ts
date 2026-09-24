/**
 * JARVIS must never take his own voice for the user's. The echo the recogniser
 * hears is rarely verbatim — these are the shapes it actually arrives in.
 */
import { collapseSpelledLetters, isSelfEchoText, stripSelfEcho } from '@/utils/selfEcho'

const REPLY =
  'Gold is trading at 4,284 dollars, down about one and a half percent today. ' +
  'The four hour chart is leaning bearish, so I would wait for a break of 4,250 before selling, Sir.'

describe('isSelfEchoText', () => {
  it.each([
    'gold is trading at 4284 dollars',                                 // clean fragment
    'the four hour chart is leaning bearish so I would wait',          // mid-reply stretch
    'four hour chart is leaning bearish so I would wait for a break',  // a word dropped at the start
    'gold is trading at 4284 dollars down about one and a hat percent',// one word misheard
    'yes sir',                                                         // short verbatim tail
  ])('treats %j as echo', t => {
    expect(isSelfEchoText(t, REPLY)).toBe(true)
  })

  it.each([
    'what about bitcoin',
    'stop, close my gold trade',
    'why is it bearish',
    'set a stop loss at 4300',
  ])('keeps the user\'s %j', t => {
    expect(isSelfEchoText(t, REPLY)).toBe(false)
  })

  it('is never echo when JARVIS has said nothing', () => {
    expect(isSelfEchoText('gold is trading', '')).toBe(false)
  })
})

describe('stripSelfEcho', () => {
  it('recovers the user\'s interruption glued onto his echo tail', () => {
    expect(stripSelfEcho('before selling sir what about bitcoin', REPLY)).toBe('what about bitcoin')
  })

  it('leaves a sentence that only shares ordinary words untouched', () => {
    expect(stripSelfEcho('is gold still bearish today', REPLY)).toBe('is gold still bearish today')
  })
})

describe('collapseSpelledLetters', () => {
  it('joins a ticker said letter by letter', () => {
    expect(collapseSpelledLetters('check x a u u s d please')).toBe('check XAUUSD please')
    expect(collapseSpelledLetters('close C.A.D.J.P.Y.')).toBe('close CADJPY')
  })
  it('leaves ordinary sentences alone', () => {
    expect(collapseSpelledLetters('I am a big fan of gold')).toBe('I am a big fan of gold')
  })
})
