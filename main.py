#!/usr/bin/env python3
"""Tiny trend bot for "Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana".

Protocol: agent-fun/bars-replay/v1. The bot speaks the line protocol directly
(hello / batch / done on stdin, one reply per batch on stdout) using only the
standard library, so there is nothing to install and nothing to fetch.

Strategy (long only, no leverage, no shorts). The dev window is a choppy,
falling market in which fast trend-following whipsaws and blind dip-buying
catches falling knives, so every entry needs a confirmation that the dip is
turning, not just that price is low:
  * Bounce-dip entry: RSI(14, Wilder) below 40 and the close below the lower
    Bollinger band (20 typical prices, 2 population sigmas), but only once
    the symbol is already bouncing (trailing 6-bar return above +1%) and the
    broad market is not in freefall (equal-weight 24-bar return above -8%).
  * MACD-dip entry: MACD(12, 26) above its signal line (9) with CCI(14) at
    or below -40 -- an oversold dip inside an uptrend, the leg that holds up
    best on the dev window.
  * Trend entry: EMA(8) crosses above EMA(21) with a positive 24-bar return
    and the broad 24-bar return above -2%.
  * Dip exits: RSI above 68, +8% take-profit, -8% stop-loss, or a 36-bar
    time stop when still under water. Trend exits: EMA(8) back below
    EMA(21), +20% take-profit, -8% stop-loss. Dip trades take quick
    profits, because in a choppy market a small locked-in gain beats a
    round trip back to the entry.
  * Sizing: each buy is equity / 7 (one equal slice per universe symbol),
    capped at 25% of equity per position and by the cash on hand, never below
    the 1 USDC minimum notional. Sells are sent first, buys after, in
    universe order, at most max_orders_per_bar orders per batch.
  * Orders decided on a bar fill at the next bar's open; max slippage is
    200 bps so a fast meme-coin move does not turn into a rejected order
    while still refusing fills into a liquidity gap.

Determinism: no randomness, no wall-clock time, no environment reads. Symbols
are always processed in the fixed universe order from hello, indicator state
is updated bar by bar in arrival order, and all money decisions use integer
micro-USDC math. The same bars therefore produce the same orders, byte for
byte, on every run.
"""

import json
import math
import sys

FAST_SPAN = 8
SLOW_SPAN = 21
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
CCI_BARS = 14
RSI_BARS = 14
BAND_BARS = 20
BAND_SIGMAS = 2
MOM_BARS = 24
BOUNCE_BARS = 6
RSI_DIP_BELOW = 40
RSI_EXIT_ABOVE = 68
CCI_DIP_AT_MOST = -40
BOUNCE_MIN_RET = 0.01
# Broad-market (equal-weight 24-bar return) gates, in percent.
BROAD_DIP_FLOOR = -8.0
BROAD_TREND_FLOOR = -2.0
# Exit thresholds, in basis points of the position's cost.
STOP_BPS = 9200         # sell at or below 92% of cost (-8%)
DIP_TAKE_BPS = 10800    # dip take-profit at 108% of cost (+8%)
TREND_TAKE_BPS = 12000  # trend take-profit at 120% of cost (+20%)
DIP_MAX_HOLD = 36       # dip time stop, in bars
MAX_SLIPPAGE_BPS = 200
BPS = 10000


def _mean(xs):
    return sum(xs) / len(xs)


class Ema:
    """One EMA: seeded as the mean of its first span of closes."""

    __slots__ = ("span", "warm", "value")

    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None

    def push(self, close):
        if self.value is not None:
            self.value += (2.0 / (self.span + 1)) * (close - self.value)
            return
        self.warm.append(close)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span
            self.warm = []


class Series:
    """Per-symbol indicator state."""

    __slots__ = ("fast", "slow", "gap", "now", "closes", "typical",
                 "last", "first", "gain", "loss",
                 "macd_warm", "macd_fast", "macd_slow", "macds", "signal")

    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.gap = None   # fast - slow after the previous bar
        self.now = None   # fast - slow after this bar
        self.closes = []  # closes, oldest first (kept to MOM_BARS + 1)
        self.typical = []  # last BAND_BARS typical prices, oldest first
        self.last = None
        self.first = []   # first RSI_BARS changes, while averages are unset
        self.gain = None
        self.loss = None
        # MACD(12, 26, 9), TA-Lib seeding: slow is the mean of the first 26
        # closes, fast the mean of the last 12 of them; the signal line is
        # the mean of the first 9 MACD values.
        self.macd_warm = []
        self.macd_fast = None
        self.macd_slow = None
        self.macds = []
        self.signal = None

    def push(self, high, low, close):
        self.fast.push(close)
        self.slow.push(close)
        self.gap = self.now
        if self.fast.value is not None and self.slow.value is not None:
            self.now = self.fast.value - self.slow.value
        else:
            self.now = None
        self.closes.append(close)
        if len(self.closes) > MOM_BARS + 1:
            self.closes.pop(0)
        self.typical.append((high + low + close) / 3.0)
        if len(self.typical) > BAND_BARS:
            self.typical.pop(0)
        if self.last is not None:
            change = close - self.last
            up = change if change > 0 else 0.0
            down = -change if change < 0 else 0.0
            if self.gain is None or self.loss is None:
                self.first.append(change)
                if len(self.first) == RSI_BARS:
                    self.gain = sum(x for x in self.first if x > 0) / RSI_BARS
                    self.loss = sum(-x for x in self.first if x < 0) / RSI_BARS
            else:
                self.gain = (self.gain * (RSI_BARS - 1) + up) / RSI_BARS
                self.loss = (self.loss * (RSI_BARS - 1) + down) / RSI_BARS
        self.last = close
        # MACD.
        if self.macd_slow is None or self.macd_fast is None:
            self.macd_warm.append(close)
            if len(self.macd_warm) == MACD_SLOW:
                self.macd_slow = _mean(self.macd_warm)
                self.macd_fast = _mean(self.macd_warm[MACD_SLOW - MACD_FAST:])
        else:
            k_f = 2.0 / (MACD_FAST + 1)
            k_s = 2.0 / (MACD_SLOW + 1)
            self.macd_fast += k_f * (close - self.macd_fast)
            self.macd_slow += k_s * (close - self.macd_slow)
        if self.macd_slow is not None and self.macd_fast is not None:
            macd = self.macd_fast - self.macd_slow
            if self.signal is not None:
                self.signal += (2.0 / (MACD_SIGNAL + 1)) * (macd - self.signal)
            else:
                self.macds.append(macd)
                if len(self.macds) == MACD_SIGNAL:
                    self.signal = _mean(self.macds)

    def rsi(self):
        if self.gain is None or self.loss is None:
            return None
        moved = self.gain + self.loss
        return 50.0 if moved == 0 else 100.0 * self.gain / moved

    def lower_band(self):
        if len(self.typical) < BAND_BARS:
            return None
        mean = sum(self.typical) / BAND_BARS
        var = sum((x - mean) * (x - mean) for x in self.typical) / BAND_BARS
        return mean - BAND_SIGMAS * math.sqrt(var)

    def cci(self):
        if len(self.typical) < CCI_BARS:
            return None
        window = self.typical[-CCI_BARS:]
        mean = sum(window) / CCI_BARS
        mad = sum(abs(x - mean) for x in window) / CCI_BARS
        if mad == 0:
            return 0.0
        return (window[-1] - mean) / (0.015 * mad)

    def macd(self):
        if self.macd_fast is None or self.macd_slow is None:
            return None
        return self.macd_fast - self.macd_slow

    def ret_n(self, n):
        """Trailing n-bar return as a fraction, or None without history."""
        if len(self.closes) <= n or self.closes[-1 - n] == 0:
            return None
        return self.closes[-1] / self.closes[-1 - n] - 1.0

    def ret_24(self):
        return self.ret_n(MOM_BARS)

    def crossed_up(self):
        return (self.gap is not None and self.now is not None
                and self.now > 0 and self.gap <= 0)

    def crossed_down(self):
        return (self.gap is not None and self.now is not None
                and self.now < 0 and self.gap >= 0)


def main():
    symbols = []          # universe order from hello
    price_scale = {}
    qty_scale = {}
    quote_scale = 6
    caps = {}
    fees = {}
    warmup_bars = 0
    series = {}
    entry_kind = {}       # symbol -> "dip" | "trend"
    entry_seq = {}        # symbol -> seq of the bar the buy was decided on
    next_id = [1]

    def client_id():
        cid = "o%d" % next_id[0]
        next_id[0] += 1
        return cid

    def position_value(symbol, qty, close_raw):
        """Value of qty at close_raw, in micro-USDC (integer math)."""
        return (
            qty
            * close_raw
            * (10 ** quote_scale)
            // (10 ** (qty_scale[symbol] + price_scale[symbol]))
        )

    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            quote_scale = msg["capital"]["quote_scale"]
            caps = msg["caps"]
            fees = msg["fees"]
            warmup_bars = msg.get("warmup_bars", 0)
            for s in msg["symbols"]:
                symbols.append(s["symbol"])
                price_scale[s["symbol"]] = s["price_scale"]
                qty_scale[s["symbol"]] = s["qty_scale"]
                series[s["symbol"]] = Series()
            continue
        if kind == "done":
            break
        if kind != "batch":
            continue

        seq = msg["seq"]
        bars = msg["bars"]
        account = msg["account"]
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        positions = {}
        for p in account["positions"]:
            positions[p["symbol"]] = (int(p["qty"]), int(p["cost_quote_units"]))

        # Fold this batch's bars into the indicators (hello, batch, orders, bar).
        latest_close = {}
        for bar in bars:
            sym = bar["symbol"]
            if sym not in series:
                continue
            ps = price_scale[sym]
            series[sym].push(int(bar["h"]) / float(10 ** ps),
                             int(bar["l"]) / float(10 ** ps),
                             int(bar["c"]) / float(10 ** ps))
            latest_close[sym] = int(bar["c"])

        if seq < warmup_bars:
            out.write(json.dumps({"type": "reply", "seq": seq, "orders": []},
                                 separators=(",", ":")) + "\n")
            out.flush()
            continue

        # Broad market: equal-weight 24-bar return across priced symbols.
        rets = []
        for sym in symbols:
            r = series[sym].ret_24()
            if r is not None:
                rets.append(r)
        broad = (sum(rets) / len(rets) * 100.0) if rets else 0.0

        max_orders = caps["max_orders_per_bar"]
        max_weight = caps["max_weight_bps"]
        min_notional = int(fees["min_notional_quote_units"])
        orders = []

        # --- Exits first.
        sells = set()
        for sym in symbols:
            held = positions.get(sym)
            if held is None or sym not in latest_close:
                continue
            qty, cost = held
            if qty <= 0 or cost <= 0:
                continue
            value = position_value(sym, qty, latest_close[sym])
            s = series[sym]
            rsi = s.rsi()
            kind_held = entry_kind.get(sym, "dip")
            sell = False
            if value * BPS <= cost * STOP_BPS:
                sell = True  # stop-loss
            elif kind_held == "dip":
                if value * BPS >= cost * DIP_TAKE_BPS:
                    sell = True  # dip take-profit
                elif rsi is not None and rsi > RSI_EXIT_ABOVE:
                    sell = True  # dip exhausted to the upside
                elif (seq - entry_seq.get(sym, seq)) >= DIP_MAX_HOLD and value < cost:
                    sell = True  # dip time stop: no recovery in 36 bars
            else:
                if value * BPS >= cost * TREND_TAKE_BPS:
                    sell = True  # trend take-profit
                elif s.crossed_down():
                    sell = True  # EMA(8) back below EMA(21)
            if sell:
                sells.add(sym)
                orders.append({
                    "side": "sell",
                    "client_id": client_id(),
                    "symbol": sym,
                    "qty": str(qty),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                })
        for sym in sells:
            entry_kind.pop(sym, None)
            entry_seq.pop(sym, None)

        # --- Entries: bounce-dip, MACD-dip, then trend, in universe order.
        n_slots = min(len(symbols), caps["max_open_positions"])
        stake = equity // n_slots if n_slots > 0 else 0
        by_weight = max_weight * equity // BPS
        room = n_slots - len(positions)
        cash_left = cash
        for sym in symbols:
            if len(orders) >= max_orders or room <= 0:
                break
            if sym in positions or sym in sells:
                continue
            if sym not in latest_close:
                continue
            s = series[sym]
            rsi = s.rsi()
            lower = s.lower_band()
            close = int(latest_close[sym]) / float(10 ** price_scale[sym])
            entry = None
            macd = s.macd()
            cci = s.cci()
            if (macd is not None and s.signal is not None and cci is not None
                    and macd > s.signal and cci <= CCI_DIP_AT_MOST):
                entry = "dip"  # oversold dip inside a MACD uptrend
            elif (rsi is not None and lower is not None
                    and rsi < RSI_DIP_BELOW and close < lower
                    and broad > BROAD_DIP_FLOOR):
                r6 = s.ret_n(BOUNCE_BARS)
                if r6 is not None and r6 > BOUNCE_MIN_RET:
                    entry = "dip"  # bounce already underway off the low
            elif (s.crossed_up() and broad > BROAD_TREND_FLOOR):
                r24 = s.ret_24()
                if r24 is not None and r24 > 0:
                    entry = "trend"
            if entry is None:
                continue
            amount = stake
            if amount > by_weight:
                amount = by_weight
            if amount > cash_left:
                amount = cash_left
            if amount < min_notional:
                continue
            orders.append({
                "side": "buy",
                "client_id": client_id(),
                "symbol": sym,
                "notional_quote_units": str(amount),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            entry_kind[sym] = entry
            entry_seq[sym] = seq
            cash_left -= amount
            room -= 1

        out.write(json.dumps({"type": "reply", "seq": seq,
                              "orders": orders[:max_orders]},
                             separators=(",", ":")) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
