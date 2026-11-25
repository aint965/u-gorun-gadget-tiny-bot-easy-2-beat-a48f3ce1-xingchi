# Tiny Trend Bot — Beat Buy-and-Hold on Solana

[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/aint965/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1-xingchi/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/aint965/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1-xingchi/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

Entry for [Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1) — a free-entry paper-trading competition on hourly Solana prices.

## Strategy

A robust multi-asset trend bot (`main.py`, Python 3 stdlib only, no network, no outside data) speaking the `agent-fun/bars-replay/v1` protocol:

- **Confirmed dip entries:** a bounce-dip (RSI(14) below 40 with the close below the lower Bollinger band, entered only once the 6-bar return turns positive) and a MACD-dip (MACD(12,26) above its signal line with CCI(14) at or below −40 — an oversold dip inside an uptrend). Both are gated by the broad market not being in freefall.
- **Trend entry:** EMA(8) crosses above EMA(21) with a positive 24-bar return and a healthy broad market.
- **Disciplined exits:** dip positions exit on RSI above 68, a +8% take-profit, a −8% stop-loss, or a 36-bar time stop when still under water; trend positions ride longer (EMA cross-down exit, +20% take-profit, −8% stop-loss).
- **Sizing:** equal slices of equity / 7 per symbol, capped at the 25% per-position weight limit and by cash on hand, never below the 1 USDC minimum notional. Long only, no leverage, no shorts.
- **Deterministic:** no randomness, no clock, no environment reads; symbols are always processed in the fixed universe order and all money decisions use integer micro-USDC math, so two runs on the same bars produce byte-identical action logs.

## Run

The grader runs `python3 -P -s main.py`; the bot reads `hello` / `batch` / `done` JSON lines on stdin and writes one `reply` per batch on stdout.
