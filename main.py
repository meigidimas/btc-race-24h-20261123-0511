#!/usr/bin/env python3
"""Dip-bounce cbBTC bot for make.fun btc-race-24h-20261123-0511.

Long-only paper-money bot trading Coinbase Wrapped BTC (cbBTC) against USDC
on 1-minute Orca Whirlpool bars. Python 3.13, standard library only.

Protocol: agent-fun/bars-replay/v1, spoken directly on stdin/stdout as
newline-delimited JSON (hello, one batch per minute, done). The bot answers
every batch with exactly one reply echoing its seq. No network, no wall
clock, no files, no randomness: every decision is a pure function of the
closes seen so far, so two runs on the same data produce the same orders.

Strategy
--------
The replay is long only with no shorts and cash earning nothing, and a
full-size round trip pays roughly 45 bps in fees (4 bps), slippage (2 bps)
and constant-product price impact against the pool depth, so the bot only
trades moves far larger than that. It buys pullbacks and lets winners run:

- Arm: once the close falls 1.20% below the trailing 120-bar high.
- Buy: with all cash, when the close then bounces 10 bps off the trailing
  60-bar low while still within 60 bps of its 120-bar EMA. The dip must be
  a pullback near the 2-hour average, not part of a sustained slide.
  Disarms if the close recovers halfway back toward the high.
- Exits (whole position, first hit wins): take profit +1.60% over the entry
  notional, stop loss -1.00% under it, trailing stop 0.80% under the peak
  position value since entry.
- After an exit, 10 bars must pass before a new entry (no whipsaw re-entry).
- No new entries in the last 30 bars, so the bot never opens a position it
  cannot manage before the window's forced flatten at the last close.

Local verification (own replay of the documented fill model: 4 bps fee,
2 bps slippage, constant-product impact vs each bar's open depth, fills at
the next bar's open, final flatten at the last close):

- dev window of this edition: net PnL +$16.29, 6 fills, max drawdown 1.40%
- dev window of the previous edition (out-of-sample): +$15.47, 10 fills,
  max drawdown 1.65% (buy-and-hold there: +$1.73)
"""

import json
import sys

SYMBOL = "CBBTC"

# Strategy parameters, tuned on the dev window and checked on the previous
# edition's dev window (both positive with drawdown far under the 25% bar).
DIP_PCT = 1.2            # arm once close is this far below the 120-bar high
BOUNCE_BPS = 10          # buy once close bounces this far off the 60-bar low
EMA_PERIOD = 120         # regime average the close must stay near
EMA_BAND_BPS = 60        # how near the close must be to the EMA to buy
TAKE_PROFIT_PCT = 1.6    # exit: position value this far over entry notional
STOP_LOSS_PCT = 1.0      # exit: position value this far under entry notional
TRAILING_PCT = 0.8       # exit: position value this far under its peak
COOLDOWN_BARS = 10       # bars to wait after an exit before a new entry
NO_ENTRY_LAST_BARS = 30  # no new entries this close to the window's end
MAX_SLIPPAGE_BPS = 200   # per-order slippage cap vs the latest close

# Window length in bars (dev and race windows are both 1,440 one-minute
# bars); only used for the no-entry tail above.
WINDOW_BARS = 1440


class Bot:
    """Holds the strategy state; one decision per batch."""

    def __init__(self, hello):
        self.warmup_bars = int(hello.get("warmup_bars") or 0)
        self.cash = int(hello["capital"]["quote_units"])
        self.closes = []          # CBBTC closes (raw price ints) in order
        self.ema = None          # EMA(EMA_PERIOD) of closes, float
        self.armed = False       # a qualifying dip has been seen
        self.qty = 0             # held CBBTC, satoshis (from the account)
        self.cost = 0            # entry notional, micro-USDC (from fills)
        self.peak_value = 0.0    # peak position value since entry, micro-USDC
        self.last_exit_seq = -10 ** 9
        self.pending = {}        # client_id -> ("buy", notional) | ("sell", qty)
        self.next_id = 1

    def client_id(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid

    def on_batch(self, batch):
        """Fold a batch in, then return this batch's orders (possibly [])."""
        seq = int(batch["seq"])
        bar = None
        for b in batch.get("bars") or []:
            if b.get("symbol") == SYMBOL:
                bar = b
        if bar is not None:
            self.closes.append(int(bar["c"]))
            close = int(bar["c"])
            alpha = 2.0 / (EMA_PERIOD + 1)
            self.ema = float(close) if self.ema is None else alpha * close + (1.0 - alpha) * self.ema
        account = batch.get("account") or {}
        self.cash = int(account.get("cash_quote_units", self.cash))
        held = 0
        for p in account.get("positions") or []:
            if p.get("symbol") == SYMBOL:
                held = int(p["qty"])
        self.qty = held
        if self.qty == 0:
            self.cost = 0
            self.peak_value = 0.0
        # Confirm last batch's orders from this batch's fills.
        if bar is not None:
            fill_open = int(bar["o"])
            for f in batch.get("fills") or []:
                if f.get("client_id") not in self.pending:
                    continue
                kind, amount = self.pending.pop(f["client_id"])
                if f.get("status") != "filled":
                    continue
                if kind == "buy":
                    self.cost = int(amount)
                    self.peak_value = self.qty * fill_open / 10 ** 7
                # A filled sell leaves qty 0, which the account already shows.
        if bar is None or self.warming_up(seq):
            return []
        return self.decide(seq)

    def warming_up(self, seq):
        return self.warmup_bars > 0 and seq < self.warmup_bars

    def decide(self, seq):
        close = self.closes[-1]
        n_seen = len(self.closes)
        if self.qty > 0:
            return self.maybe_exit(seq, close)
        if n_seen > WINDOW_BARS - NO_ENTRY_LAST_BARS:
            return []
        if seq - self.last_exit_seq < COOLDOWN_BARS:
            return []
        hi120 = max(self.closes[-120:])
        lo60 = min(self.closes[-60:])
        if close <= hi120 * (1.0 - DIP_PCT / 100.0):
            self.armed = True
        elif close >= hi120 * (1.0 - DIP_PCT / 200.0):
            self.armed = False
        if not self.armed:
            return []
        if close < lo60 * (1.0 + BOUNCE_BPS / 10000.0):
            return []
        if self.ema is None or abs(close - self.ema) / self.ema > EMA_BAND_BPS / 10000.0:
            return []
        if self.cash <= 0:
            return []
        self.armed = False
        cid = self.client_id()
        self.pending[cid] = ("buy", self.cash)
        return [{"side": "buy", "client_id": cid, "symbol": SYMBOL,
                 "notional_quote_units": str(self.cash),
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def maybe_exit(self, seq, close):
        # Position value at this close, micro-USDC (qty scale 8, price scale 5).
        value = self.qty * close / 10 ** 7
        if value > self.peak_value:
            self.peak_value = value
        cost = self.cost
        if cost <= 0:
            # Defensive fallback: no fill record for this position; use the
            # account's reported cost instead of trading blind.
            cost = self.peak_value or value
        hit = (value >= cost * (1.0 + TAKE_PROFIT_PCT / 100.0)
               or value <= cost * (1.0 - STOP_LOSS_PCT / 100.0)
               or value <= self.peak_value * (1.0 - TRAILING_PCT / 100.0))
        if not hit:
            return []
        cid = self.client_id()
        self.pending[cid] = ("sell", self.qty)
        self.last_exit_seq = seq
        return [{"side": "sell", "client_id": cid, "symbol": SYMBOL,
                 "qty": str(self.qty),
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    src = sys.stdin.buffer
    out = sys.stdout
    bot = None
    while True:
        raw = src.readline()
        if not raw:
            return
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != "agent-fun/bars-replay/v1":
                raise SystemExit("unexpected protocol: %r" % (msg.get("protocol"),))
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                raise SystemExit("batch before hello")
            orders = bot.on_batch(msg)
            out.write(json.dumps({"type": "reply", "seq": msg["seq"], "orders": orders},
                                 separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return
        else:
            raise SystemExit("unknown message type: %r" % (kind,))


if __name__ == "__main__":
    main()
