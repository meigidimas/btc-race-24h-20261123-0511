# btc-race-24h-20261123-0511 — Dip-Bounce cbBTC Bot

[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/meigidimas/btc-race-24h-20261123-0511/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/meigidimas/btc-race-24h-20261123-0511/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

A long-only cbBTC/USDC trading bot for make.fun's `btc-race-24h` competition
(`agent-fun/bars-replay/v1`, Python 3.13, standard library only). Paper money
only: it starts with 1,000 USDC and trades 1-minute on-chain bars. Nothing is
traded for real.

## Strategy

A full-size round trip pays roughly 45 bps in fees (4 bps), slippage (2 bps)
and constant-product price impact against the pool's open depth, so the bot
only trades moves far larger than that. It buys pullbacks and lets winners
run with a trailing stop:

- **Arm.** Once the close falls 1.20% below the trailing 120-bar high.
- **Buy.** With all cash, when the close then bounces 10 bps off the trailing
  60-bar low while still within 60 bps of its 120-bar EMA. The dip must be a
  pullback near the 2-hour average, not part of a sustained slide. It disarms
  if the close recovers halfway back toward the high.
- **Exits (whole position, first hit wins).** Take profit +1.60% over the entry
  notional, stop loss -1.00% under it, trailing stop 0.80% under the peak
  position value since entry.
- **Cooldown.** 10 bars must pass after an exit before a new entry, so the bot
  never whipsaws straight back into the same dip.
- **No new entries in the last 30 bars**, so the bot never opens a position it
  cannot manage before the window's forced flatten at the last close.

The bot is deterministic: every decision is a pure function of the closes
seen so far — no clock, no randomness, no network, no files. Two runs on the
same data produce the same orders (verified: byte-identical action logs).

## Files

- `main.py` — the bot (entrypoint). Speaks `agent-fun/bars-replay/v1`
  directly on stdin/stdout: hello, one batch per minute, done; exactly one
  reply per batch echoing its seq. No vendored shim — the wire protocol is
  implemented inline in the entrypoint.
- `LICENSE` — MIT.

## Running it

The grader runs `python3 -P -s /bot/main.py` with `PYTHONHASHSEED=0`. To
smoke-test the protocol by hand, pipe a hello plus one batch in and check
that exactly one `{"type":"reply",...}` line comes back per batch.

## Local verification

Verified with an end-to-end replay of the documented fill model (4 bps fee,
2 bps slippage, constant-product impact vs each bar's open depth, fills at
the next bar's open, final flatten at the last close), driving `main.py`
through the real protocol twice:

| window | net PnL | fills | max drawdown |
| --- | --- | --- | --- |
| dev window (this edition) | +$16.29 | 6 | 1.40% |
| dev window (previous edition, out-of-sample) | +$15.47 | 9 (+ flatten) | 1.65% |

The dev gate needs net PnL above $0, so this entry clears it with margin on
both windows, with drawdown far under the 25% bar. (Buy-and-hold on this
edition's dev window: +$31.73, but it fails the gate on a falling window;
this bot is built to survive either direction.)
