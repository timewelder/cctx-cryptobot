"""
risk_manager.py
----------------
Module 3: Risk Management Engine.

RiskManager decides *whether* and *how much* to trade - it never places
orders itself (that's ExecutionEngine's job). It configures exchange
leverage, sizes positions from the standalone ATR(14) so that a stop hit
costs exactly `risk_per_trade` of account balance, validates that the
required margin is actually available, and provides the emergency
hard-stop check used when an exchange-side stop-loss may have failed to
trigger (e.g. during a flash crash / liquidity gap).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import ccxt

from utils import with_retries


@dataclass(frozen=True)
class PositionSizeResult:
    size_base: float        # quantity of the base asset/contracts to send to the exchange
    notional_usd: float     # USD value of that position at current price
    margin_required: float  # notional_usd / leverage
    risk_amount_usd: float  # USD at risk if the stop-loss is hit
    stop_distance: float    # price distance to the stop (2 * ATR)


class RiskManager:
    def __init__(
        self,
        exchange,
        symbol: str,
        leverage: int,
        risk_per_trade: float,
        margin_safety_buffer: float = 0.95,
        logger: logging.Logger | None = None,
    ):
        self.exchange = exchange
        self.symbol = symbol
        self.leverage = leverage
        self.risk_per_trade = risk_per_trade
        self.margin_safety_buffer = margin_safety_buffer
        self.log = logger or logging.getLogger("trading_bot")

    async def configure_leverage(self) -> None:
        """Set leverage for this symbol on startup. Failures are logged
        but not fatal - some exchanges reject this call if the leverage
        is already at the requested value or need margin-mode set first,
        neither of which should stop the bot from starting."""
        try:
            await with_retries(self.exchange.set_leverage, self.leverage, self.symbol, logger=self.log)
            self.log.info("Leverage set to %sx for %s", self.leverage, self.symbol)
        except ccxt.ExchangeError as e:
            self.log.warning(
                "set_leverage failed (%s) - verify manually that %s is configured for %sx "
                "leverage on %s before trading live.",
                e, self.symbol, self.leverage, self.exchange.id,
            )

    def calculate_position_size(self, account_balance: float, atr: float, current_price: float) -> PositionSizeResult:
        """Position Size (base) = (Account Balance * risk_per_trade) / (2 * ATR)

        Units matter here: risk_amount is in quote currency (USD), and
        `2 * ATR` is a price distance (also quote currency, per 1 unit of
        base asset) - so risk_amount / stop_distance yields a *base asset
        quantity* (e.g. BTC), not a USD figure. We compute both below:
        size_base is what actually goes into the exchange order, and
        notional_usd/margin_required is what the margin check needs.
        """
        if atr <= 0:
            raise ValueError(f"ATR must be positive to size a stop-based position, got {atr}")
        if current_price <= 0:
            raise ValueError(f"current_price must be positive, got {current_price}")

        risk_amount = account_balance * self.risk_per_trade
        stop_distance = 2 * atr
        size_base = risk_amount / stop_distance
        notional_usd = size_base * current_price
        margin_required = notional_usd / self.leverage

        return PositionSizeResult(
            size_base=size_base,
            notional_usd=notional_usd,
            margin_required=margin_required,
            risk_amount_usd=risk_amount,
            stop_distance=stop_distance,
        )

    def validate_margin(self, sizing: PositionSizeResult, available_margin: float) -> bool:
        """Ensure notional / leverage doesn't exceed available margin,
        with a safety buffer left over for fees and adverse slippage on
        the marketable-limit entry."""
        usable = available_margin * self.margin_safety_buffer
        ok = sizing.margin_required <= usable
        if not ok:
            self.log.warning(
                "Position rejected: needs $%.2f margin but only $%.2f usable "
                "(available $%.2f x %.0f%% safety buffer).",
                sizing.margin_required, usable, available_margin, self.margin_safety_buffer * 100,
            )
        return ok

    def check_emergency_stop(
        self,
        position_side: str,
        entry_price: float,
        stop_price: float,
        current_price: float,
        stop_order_status: str | None,
    ) -> bool:
        """Return True if a hard, immediate market close is needed: price
        has already blown through the intended stop level AND the
        exchange-side stop order has not reported as filled/closed. This
        is the backstop for scenarios where the book gaps straight
        through the stop (flash crash) or the conditional order never
        triggers - the exchange-side stop_market order remains the
        primary defense in normal conditions.
        """
        if stop_order_status in ("closed", "filled"):
            return False

        if position_side == "long" and current_price <= stop_price:
            self.log.critical(
                "EMERGENCY STOP: long stop %.6f breached (price %.6f), stop order status=%s",
                stop_price, current_price, stop_order_status,
            )
            return True
        if position_side == "short" and current_price >= stop_price:
            self.log.critical(
                "EMERGENCY STOP: short stop %.6f breached (price %.6f), stop order status=%s",
                stop_price, current_price, stop_order_status,
            )
            return True
        return False
