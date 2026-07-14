"""
main.py
-------
Module 5: Main Loop & Orchestration.

Wires DataManager, StrategyEngine, RiskManager, and ExecutionEngine
together into a loop that wakes up once per 15-minute candle close,
checks for signals or manages an existing position, and logs
performance metrics.

Run:   python main.py
Stop:  Ctrl+C - the bot finishes the current cycle and closes the
       exchange connection cleanly before exiting.
"""

from __future__ import annotations

import asyncio
import signal

import ccxt

from config import settings
from data_manager import DataManager
from execution_engine import ExecutionEngine
from risk_manager import RiskManager
from strategy_engine import StrategyConfig, StrategyEngine
from utils import seconds_until_next_candle_close, setup_logging, with_retries


class TradingBot:
    def __init__(self):
        settings.validate()
        self.log = setup_logging(settings.log_path)
        self.data = DataManager(settings, logger=self.log)
        self.strategy = StrategyEngine(
            StrategyConfig(
                atr_period=settings.atr_period,
                rsi_period=settings.rsi_period,
                supertrend_period=settings.supertrend_period,
                supertrend_multiplier=settings.supertrend_multiplier,
                atr_volatility_threshold=settings.atr_volatility_threshold,
            ),
            logger=self.log,
        )
        self.risk = RiskManager(
            self.data.exchange, settings.symbol, settings.leverage,
            settings.risk_per_trade, settings.margin_safety_buffer, logger=self.log,
        )
        self.execution = ExecutionEngine(
            self.data.exchange, settings.symbol, settings.limit_order_offset_pct,
            settings.fill_check_delay_s, logger=self.log,
        )
        self._stop_requested = False

    async def startup(self) -> None:
        await self.data.load_markets()
        await self.risk.configure_leverage()
        self.log.info(
            "TradingBot started: %s %s on %s, %sx leverage, risk %.2f%%/trade, testnet=%s",
            settings.symbol, settings.timeframe, settings.exchange_id,
            settings.leverage, settings.risk_per_trade * 100, settings.testnet,
        )

    async def shutdown(self) -> None:
        self.log.info("Shutting down - closing exchange connection.")
        await self.data.close()

    async def _get_open_position(self) -> dict | None:
        try:
            positions = await with_retries(self.data.exchange.fetch_positions, [settings.symbol], logger=self.log)
        except ccxt.ExchangeError as e:
            self.log.error("fetch_positions failed: %s", e)
            return None
        for p in positions:
            if float(p.get("contracts") or 0) != 0:
                return p
        return None

    async def run_once(self) -> None:
        df = await self.data.fetch_ohlcv()
        signals = self.strategy.generate_signals(df)
        latest = signals.iloc[-1]

        balance_info = await with_retries(self.data.exchange.fetch_balance, logger=self.log)
        # Assumes a USDT-margined account (true for binanceusdm defaults
        # and Bybit linear USDT perps) - adjust the currency key if you
        # trade a coin-margined / inverse contract instead.
        total_balance = float(balance_info.get("USDT", {}).get("total") or 0.0)
        free_margin = float(balance_info.get("USDT", {}).get("free") or 0.0)
        self.data.record_equity(total_balance)

        position = await self._get_open_position()
        if position is not None:
            await self._manage_open_position(position, latest)
        else:
            await self._maybe_enter(latest, total_balance, free_margin)

        summary = self.data.get_performance_summary()
        self.log.info(
            "Performance | Realized PnL: $%.2f | Win/Loss: %s/%s (ratio %.2f) | Max DD: %.2f%%",
            summary["realized_pnl"], summary["wins"], summary["losses"],
            summary["win_loss_ratio"], summary["max_drawdown_pct"],
        )

    async def _maybe_enter(self, latest, total_balance: float, free_margin: float) -> None:
        side = "long" if latest["long_entry"] else "short" if latest["short_entry"] else None
        if side is None:
            self.log.info(
                "No entry signal this candle (close=%.4f, rsi=%.2f, atr=%.4f).",
                latest["close"], latest["rsi"], latest["atr"],
            )
            return

        sizing = self.risk.calculate_position_size(total_balance, latest["atr"], latest["close"])
        if not self.risk.validate_margin(sizing, free_margin):
            return

        order_side = "buy" if side == "long" else "sell"
        fill = await self.execution.place_marketable_limit_order(order_side, sizing.size_base)
        if fill.filled_amount <= 0:
            self.log.info("Entry order for %s did not fill - skipping this candle.", side)
            return

        stop_price = (
            latest["close"] - sizing.stop_distance if side == "long"
            else latest["close"] + sizing.stop_distance
        )
        await self.execution.place_stop_loss(side, fill.filled_amount, stop_price)

        trade_id = self.data.save_open_trade(
            side, latest["close"], fill.filled_amount, sizing.notional_usd, stop_price
        )
        self.data.save_state("open_trade", {
            "trade_id": trade_id, "side": side, "entry_price": float(latest["close"]),
            "size_base": fill.filled_amount, "stop_price": stop_price,
        })
        self.log.info(
            "Entered %s %s: size=%.6f notional=$%.2f stop=%.4f (trade #%d)",
            side, settings.symbol, fill.filled_amount, sizing.notional_usd, stop_price, trade_id,
        )

    async def _manage_open_position(self, position: dict, latest) -> None:
        state = self.data.load_state("open_trade")
        if state is None:
            self.log.warning(
                "Exchange reports an open position but local state has no record of it - "
                "skipping automated management this candle. Check manually."
            )
            return

        position_side = state["side"]
        stop_price = state["stop_price"]
        current_price = float(latest["close"])

        open_orders = await with_retries(self.data.exchange.fetch_open_orders, settings.symbol, logger=self.log)
        # Best-effort match for our stop order; reduceOnly visibility on
        # the unified order dict can vary by exchange - confirm against
        # your exchange's actual response shape on testnet.
        stop_order = next((o for o in open_orders if o.get("reduceOnly")), None)
        stop_status = stop_order["status"] if stop_order else None

        if self.risk.check_emergency_stop(position_side, state["entry_price"], stop_price, current_price, stop_status):
            result = await self.execution.market_close_position(position_side, state["size_base"])
            exit_price = float(result.get("average") or current_price)
            pnl = (
                (exit_price - state["entry_price"]) * state["size_base"] if position_side == "long"
                else (state["entry_price"] - exit_price) * state["size_base"]
            )  # excludes fees/funding - add your exchange's fee schedule for exact accounting
            self.data.close_trade(state["trade_id"], exit_price, pnl)
            self.data.save_state("open_trade", None)
            self.log.critical(
                "Emergency close executed for trade #%d: exit=%.4f pnl=%.2f",
                state["trade_id"], exit_price, pnl,
            )
            return

        if stop_order is None:
            self.log.warning("No stop order found for open trade #%d - re-placing.", state["trade_id"])
            await self.execution.place_stop_loss(position_side, state["size_base"], stop_price)

    async def main_loop(self) -> None:
        await self.startup()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                pass  # signal handlers aren't available on every platform

        try:
            while not self._stop_requested:
                sleep_s = seconds_until_next_candle_close(settings.timeframe)
                self.log.info("Sleeping %.1fs until next %s candle close.", sleep_s, settings.timeframe)
                await asyncio.sleep(sleep_s)
                if self._stop_requested:
                    break
                try:
                    await self.run_once()
                except Exception:
                    self.log.exception("Unhandled error in run_once - continuing to next candle.")
        finally:
            await self.shutdown()

    def _request_stop(self) -> None:
        self.log.info("Stop requested - will exit after the current cycle.")
        self._stop_requested = True


if __name__ == "__main__":
    bot = TradingBot()
    asyncio.run(bot.main_loop())
