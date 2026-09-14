---
type: voice-identity
updated: 2026-09-14T20:42:03.769271+00:00
sessions: 164
words_learned: 474
tags:
  - jarvis
  - voice
  - identity
---

# JARVIS Voice Identity

> This note is the permanent voice memory for JARVIS.  It is **never deleted** — only improved over time as you speak.

Updated: 2026-09-14 20:42 UTC  | Words learned: **474**  | Voice sessions: **164**

## Voice Frequency Fingerprint
| Band | Mean Energy | Std Dev |
| ---- | ----------- | ------- |
| <80Hz | 0.9990 | 0.0311 |
| 80-160 | 0.3241 | 0.0726 |
| 160-320 | 0.1246 | 0.0619 |
| 320-640 | 0.0567 | 0.0500 |
| 640Hz-1.3k | 0.0129 | 0.0325 |
| 1.3-2.5k | 0.0020 | 0.0300 |
| 2.5-5k | 0.0000 | 0.0300 |
| 5-10k | 0.0000 | 0.0300 |
| 10-16k | 0.0000 | 0.0300 |
| 16-20k | 0.0000 | 0.0300 |
| 20k+ | 0.0000 | 0.0300 |
| ultra | 0.0000 | 0.0300 |

Spectral centroid: **0.0391**  Sessions: **164**

## Top Learned Words (speech vocabulary)

| Word | Times Spoken |
| ---- | ------------ |
| jarvis | 429 |
| analyze | 421 |
| gold | 420 |
| live | 420 |
| open | 416 |
| market | 410 |
| symbol | 408 |
| analysis | 405 |
| take | 405 |
| navigate | 404 |
| dashboard | 404 |
| confirm | 403 |
| entry | 403 |
| setups | 403 |
| volume | 403 |
| bullish | 403 |
| setup | 402 |
| risk | 402 |
| sell | 401 |
| position | 401 |
| account | 401 |
| bitcoin | 401 |
| bias | 401 |
| execute | 400 |
| close | 400 |
| limit | 400 |
| orders | 400 |
| stop | 400 |
| signal | 400 |
| bearish | 400 |
| momentum | 400 |
| scroll | 400 |
| chart | 400 |
| trade | 400 |
| cancel | 399 |
| buy | 399 |
| analyse | 399 |
| sniper | 399 |
| order | 399 |
| positions | 399 |
| entries | 399 |
| loss | 399 |
| profit | 399 |
| signals | 399 |
| lot | 399 |
| lots | 399 |
| pips | 399 |
| balance | 399 |
| equity | 399 |
| margin | 399 |

## How This Works

JARVIS builds a 12-band frequency fingerprint of your voice from the Web Audio API.
Each time you speak a confirmed command, the fingerprint is updated with a 10 % blend
(exponential moving average) so your natural day-to-day variation is captured without
overwriting previous learning. The vocabulary table accumulates word counts across all
sessions — words you say often get higher recognition priority.

*Machine-readable data is stored alongside this note in `voice-profile.json`.*
