import { describe, it, expect } from 'vitest'
import { phoneticWakeMatch, stripWakePhrase } from '@/utils/voiceCommands'

describe('wake word', () => {
  const WAKE: [string, string][] = [
    ['jarvis analyse gold', 'analyse gold'],
    ['Jarvis, analyse gold', 'analyse gold'],
    ['hey jarvis whats the price', 'whats the price'],
    ['um jarvis close all positions', 'close all positions'],
    ['so paul show me signals', 'show me signals'],
    ['okay so jarvis open trading', 'open trading'],
    ['sox what is bitcoin doing', 'what is bitcoin doing'],
    ['socks show balance', 'show balance'],
    ['s o x show balance', 'show balance'],
    ['S.O.X. show balance', 'show balance'],
    ['jervis whats up', 'whats up'],
    ['paul', ''],
  ]
  it.each(WAKE)('wakes on %j', (input, cmd) => {
    expect(phoneticWakeMatch(input)).toBe(true)
    expect(stripWakePhrase(input)).toBe(cmd)
  })

  const NO_WAKE = [
    'i told my friend paul about it',
    'the price of bitcoin is up',
    'please send this to paul tomorrow',
    'six of them',
    'the box is open',
  ]
  it.each(NO_WAKE)('does not wake on %j', (input) => {
    expect(phoneticWakeMatch(input)).toBe(false)
  })

  it('strict mode needs a greeting', () => {
    expect(phoneticWakeMatch('jarvis analyse gold', true)).toBe(false)
    expect(phoneticWakeMatch('hey jarvis analyse gold', true)).toBe(true)
    expect(stripWakePhrase('hey jarvis analyse gold', true)).toBe('analyse gold')
    expect(stripWakePhrase('jarvis analyse gold', true)).toBe('jarvis analyse gold')
  })
})
