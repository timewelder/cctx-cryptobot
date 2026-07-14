# 15m Multi-Indicator Futures Trading Bot

A modular Python bot for 15-minute Supertrend + RSI + ATR futures trading
on Binance USDM or Bybit linear perpetuals via CCXT, with 1%-risk position
sizing, marketable-limit execution, and an emergency hard stop-loss backstop.

## ⚠️ Before you run this with real money

This implements the spec end-to-end to production-code standards (typed,
logged, error-handled) - but **"production-grade code" is not the same as
"a validated trading strategy."** Please, before allocating real capital:

1. **Backtest first.** Nothing here validates whether Supertrend(10,3) +
   RSI(14) + ATR(14) is actually profitable on your symbol/timeframe -
   `StrategyEngine` computes signals, it doesn't tell you if they're good.
2. **Paper trade / testnet.** `TESTNET=true` is the default. Run it against
   your exchange's futures testnet through a range of real market
   conditions before ever switching to a live account.
3. **Start small.** Leverage amplifies gains and losses equally, and no
   amount of error handling eliminates exchange outages, slippage, or gaps.
4. **This isn't financial advice** - I'm not a financial advisor, and the
   strategy logic here is exactly what was specified, not a recommendation.

## Project layout

| File | Responsibility |
|---|---|
| `config.py` | Loads all settings from `.env` into a typed `Settings` object |
| `utils.py` | Logging setup, async retry/backoff, candle-close alignment |
| `data_manager.py` | CCXT connection, OHLCV fetch + gap detection/fill, SQLite persistence |
| `strategy_engine.py` | ATR / RSI / Supertrend + entry signal generation |
| `risk_manager.py` | Leverage config, position sizing, margin checks, emergency-stop logic |
| `execution_engine.py` | Marketable limit orders, partial-fill handling, timeout-safe placement |
| `main.py` | Orchestrates everything in the 15-minute loop |
| `sanity_check.py` | Offline test of the indicator math + gap logic - no API keys needed |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your API key/secret and risk settings
python sanity_check.py   # confirms the core logic runs cleanly first
python main.py
```

## Running continuously on a VPS

**Option A - Docker** (no local Python setup needed on the VPS):
```bash
docker build -t crypto-bot .
docker run -d --restart unless-stopped --env-file .env --name crypto-bot crypto-bot
docker logs -f crypto-bot
```

**Option B - systemd service:**
```ini
# /etc/systemd/system/crypto-bot.service
[Unit]
Description=15m Multi-Indicator Futures Trading Bot
After=network.target

[Service]
WorkingDirectory=/opt/crypto-bot
ExecStart=/usr/bin/python3 /opt/crypto-bot/main.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/crypto-bot/.env

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now crypto-bot
journalctl -u crypto-bot -f
```

## Design notes worth knowing

- **Position sizing units.** `(Balance * 1%) / (2*ATR)` produces a
  *base-asset quantity* (e.g. BTC), not a USD figure - `risk_amount` is in
  USD, `2*ATR` is a price distance, so risk_amount ÷ price-distance =
  quantity. `RiskManager.calculate_position_size` returns both `size_base`
  (for the order) and `notional_usd` (for the margin check).
- **Two different ATRs.** The standalone `ATR(14)` (used for the
  volatility filter and stop sizing) is separate from the ATR Supertrend
  computes internally from its own `period=10` - that's standard practice,
  not a bug, but worth knowing if you go looking for a single "ATR" value.
- **No look-ahead bias.** `DataManager` drops the last candle from every
  fetch if it hasn't closed yet (using exchange server time, corrected for
  local clock drift), so `StrategyEngine` only ever sees fully-formed bars.
- **Idempotent order placement.** Every order gets a client order ID, so
  if a network timeout happens mid-request, the bot checks open/closed
  orders for that ID before deciding whether to retry - it won't
  double-place an order just because the response got lost.
- **Stop-loss order params vary by exchange/ccxt version** (`stopPrice` vs
  `triggerPrice`, `stop_market` vs `stop` order types). Verify the exact
  params your exchange expects on testnet - `place_stop_loss` in
  `execution_engine.py` is the one place you may need to tweak.
- **The emergency stop is a backstop, not the primary defense.** The real
  stop-loss is the exchange-side `stop_market` order placed right after
  entry; `RiskManager.check_emergency_stop` only fires a market close if
  that order's status isn't closed/filled and price has already blown
  through the stop level.
- **PnL figures exclude fees and funding.** Add your exchange's fee
  schedule if you want exact accounting.

## Suggested next steps

- A backtesting module built against the same `StrategyEngine` code, so
  live and backtested logic never drift apart.
- Trailing stops / partial take-profit, if you want to let winners run.
- A monitoring/alerting hook (Telegram, Discord, email) specifically for
  the emergency-stop path, since that's the scenario you most want to
  know about immediately.
