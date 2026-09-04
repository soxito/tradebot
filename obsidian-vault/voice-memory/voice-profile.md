---
type: voice-identity
updated: 2026-09-04T19:04:43.427864+00:00
sessions: 4
words_learned: 474
tags:
  - jarvis
  - voice
  - identity
---

# JARVIS Voice Identity

> This note is the permanent voice memory for JARVIS.  It is **never deleted** — only improved over time as you speak.

Updated: 2026-09-04 19:04 UTC  | Words learned: **474**  | Voice sessions: **4**

## Voice Frequency Fingerprint
| Band | Mean Energy | Std Dev |
| ---- | ----------- | ------- |
| <80Hz | 1.0000 | 0.0300 |
| 80-160 | 0.3544 | 0.0743 |
| 160-320 | 0.0751 | 0.0598 |
| 320-640 | 0.0164 | 0.0344 |
| 640Hz-1.3k | 0.0053 | 0.0312 |
| 1.3-2.5k | 0.0004 | 0.0300 |
| 2.5-5k | 0.0000 | 0.0300 |
| 5-10k | 0.0000 | 0.0300 |
| 10-16k | 0.0000 | 0.0300 |
| 16-20k | 0.0000 | 0.0300 |
| 20k+ | 0.0000 | 0.0300 |
| ultra | 0.0000 | 0.0300 |

Spectral centroid: **0.0330**  Sessions: **4**

## Top Learned Words (speech vocabulary)

| Word | Times Spoken |
| ---- | ------------ |
| jarvis | 423 |
| analyze | 415 |
| gold | 414 |
| live | 414 |
| open | 410 |
| market | 404 |
| symbol | 402 |
| analysis | 399 |
| take | 399 |
| navigate | 398 |
| dashboard | 398 |
| confirm | 397 |
| entry | 397 |
| setups | 397 |
| volume | 397 |
| bullish | 397 |
| setup | 396 |
| risk | 396 |
| sell | 395 |
| position | 395 |
| account | 395 |
| bitcoin | 395 |
| bias | 395 |
| execute | 394 |
| close | 394 |
| limit | 394 |
| orders | 394 |
| stop | 394 |
| signal | 394 |
| bearish | 394 |
| momentum | 394 |
| scroll | 394 |
| chart | 394 |
| trade | 394 |
| cancel | 393 |
| buy | 393 |
| analyse | 393 |
| sniper | 393 |
| order | 393 |
| positions | 393 |
| entries | 393 |
| loss | 393 |
| profit | 393 |
| signals | 393 |
| lot | 393 |
| lots | 393 |
| pips | 393 |
| balance | 393 |
| equity | 393 |
| margin | 393 |

## How This Works

JARVIS builds a 12-band frequency fingerprint of your voice from the Web Audio API.
Each time you speak a confirmed command, the fingerprint is updated with a 10 % blend
(exponential moving average) so your natural day-to-day variation is captured without
overwriting previous learning. The vocabulary table accumulates word counts across all
sessions — words you say often get higher recognition priority.

*Machine-readable data is stored alongside this note in `voice-profile.json`.*
