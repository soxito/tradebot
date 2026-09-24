---
type: voice-identity
updated: 2026-09-24T16:47:26.095450+00:00
sessions: 505
words_learned: 475
tags:
  - jarvis
  - voice
  - identity
---

# JARVIS Voice Identity

> This note is the permanent voice memory for JARVIS.  It is **never deleted** — only improved over time as you speak.

Updated: 2026-09-24 16:47 UTC  | Words learned: **475**  | Voice sessions: **505**

## Voice Frequency Fingerprint
| Band | Mean Energy | Std Dev |
| ---- | ----------- | ------- |
| <80Hz | 0.9943 | 0.0358 |
| 80-160 | 0.3531 | 0.1238 |
| 160-320 | 0.1165 | 0.0769 |
| 320-640 | 0.0332 | 0.0396 |
| 640Hz-1.3k | 0.0203 | 0.0351 |
| 1.3-2.5k | 0.0122 | 0.0335 |
| 2.5-5k | 0.0024 | 0.0303 |
| 5-10k | 0.0019 | 0.0302 |
| 10-16k | 0.0017 | 0.0302 |
| 16-20k | 0.0011 | 0.0300 |
| 20k+ | 0.0002 | 0.0300 |
| ultra | 0.0000 | 0.0300 |

Spectral centroid: **0.0432**  Sessions: **505**

## Top Learned Words (speech vocabulary)

| Word | Times Spoken |
| ---- | ------------ |
| jarvis | 471 |
| analyze | 463 |
| gold | 463 |
| live | 462 |
| open | 458 |
| market | 452 |
| symbol | 450 |
| analysis | 447 |
| take | 447 |
| navigate | 446 |
| dashboard | 446 |
| confirm | 445 |
| entry | 445 |
| setups | 445 |
| volume | 445 |
| bullish | 445 |
| setup | 444 |
| risk | 444 |
| sell | 443 |
| position | 443 |
| account | 443 |
| bitcoin | 443 |
| bias | 443 |
| execute | 442 |
| close | 442 |
| limit | 442 |
| orders | 442 |
| stop | 442 |
| signal | 442 |
| bearish | 442 |
| momentum | 442 |
| scroll | 442 |
| chart | 442 |
| trade | 442 |
| cancel | 441 |
| buy | 441 |
| analyse | 441 |
| sniper | 441 |
| order | 441 |
| positions | 441 |
| entries | 441 |
| loss | 441 |
| profit | 441 |
| signals | 441 |
| lot | 441 |
| lots | 441 |
| pips | 441 |
| balance | 441 |
| equity | 441 |
| margin | 441 |

## How This Works

JARVIS builds a 12-band frequency fingerprint of your voice from the Web Audio API.
Each time you speak a confirmed command, the fingerprint is updated with a 10 % blend
(exponential moving average) so your natural day-to-day variation is captured without
overwriting previous learning. The vocabulary table accumulates word counts across all
sessions — words you say often get higher recognition priority.

*Machine-readable data is stored alongside this note in `voice-profile.json`.*
