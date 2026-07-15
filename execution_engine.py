"""
execution_engine.py
--------------------
Module 4: Execution Engine.

Places marketable limit orders instead of raw market orders (buy limit
slightly above ask, sell limit slightly below bid) to guarantee a fill
under normal conditions while still capping slippage with a hard price.
Handles partial fills after a 30-second grace period, and - critically -
never blindly retries an order after a network timeout without first
checking whether the original request actually reached the exchange.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

import ccxt

from utils import with_retries


@dataclass
class FillResult:
    order_id: str
    status: str  # 'closed' (fully filled), 'partial', 'canceled'
    filled_amount: float
    average_price: float | None


class ExecutionEngine:
    def __init__(
        self,
        exchange,
        symbol: str,
        offset_pct: float = 0.0005,
        fill_check_delay_s: float = 30.0,
        logger: logging.Logger | None = None,
    ):
        self.exchange = exchange
        self.symbol = symbol
        self.offset_pct = offset_pct
        self.fill_check_delay_s = fill_check_delay_s
        self.log = logger or logging.getLogger("trading_bot")

    async def _best_bid_ask(self) -> tuple[float, float]:
        ticker = await with_retries(self.exchange.fetch_ticker, self.symbol, logger=self.log)
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask:
            raise RuntimeError(f"Ticker for {self.symbol} missing bid/ask: {ticker}")
        return bid, ask

    async def place_marketable_limit_order(self, side: str, amount: float) -> FillResult:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        bid, ask = await self._best_bid_ask()
        price = ask * (1 + self.offset_pct) if side == "buy" else bid * (1 - self.offset_pct)

        amount = float(self.exchange.amount_to_precision(self.symbol, amount))
        price = float(self.exchange.price_to_precision(self.symbol, price))
        client_order_id = f"bot-{uuid.uuid4().hex[:20]}"

        order = await self._create_order_safely(side, amount, price, client_order_id)
        return await self.check_order_fill_status(order["id"])

    async def _create_order_safely(self, side: str, amount: float, price: float, client_order_id: str) -> dict:
        """Place the order with NO internal auto-retry (retries=0): if a
        network error happens here, we must check whether it actually
        went through before deciding to retry, rather than risk placing
        a duplicate order on top of one that silently succeeded."""
        params = {"clientOrderId": client_order_id}
        try:
            return await with_retries(
                self.exchange.create_order, self.symbol, "limit", side, amount, price, params,
                retries=0, logger=self.log,
            )
        except ccxt.NetworkError as e:
            self.log.warning(
                "Network error placing order (clientOrderId=%s): %s - checking whether it "
                "reached the exchange before retrying.", client_order_id, e,
            )
            reconciled = await self._reconcile_order_by_client_id(client_order_id)
            if reconciled is not None:
                self.log.info("Order %s was actually accepted despite the network error.", reconciled["id"])
                return reconciled

            self.log.info("No trace of the order on the exchange - retrying once.")
            return await with_retries(
                self.exchange.create_order, self.symbol, "limit", side, amount, price, params,
                retries=1, logger=self.log,
            )

    async def _reconcile_order_by_client_id(self, client_order_id: str) -> dict | None:
        """Check open orders and closed/historical orders for our client
        order id before assuming a timed-out request actually failed.
        (fetch_my_trades is an alternative cross-check, but clientOrderId
        visibility on trade-level records is less consistent across
        exchanges than on order-level records, so order lookups are the
        primary check here.)
        """
        try:
            open_orders = await with_retries(self.exchange.fetch_open_orders, self.symbol, logger=self.log)
            for o in open_orders:
                if o.get("clientOrderId") == client_order_id:
                    return o
        except ccxt.ExchangeError as e:
            self.log.warning("fetch_open_orders failed during reconciliation: %s", e)

        try:
            closed_orders = await with_retries(self.exchange.fetch_closed_orders, self.symbol, logger=self.log)
            for o in closed_orders:
                if o.get("clientOrderId") == client_order_id:
                    return o
        except ccxt.ExchangeError as e:
            self.log.warning("fetch_closed_orders failed during reconciliation: %s", e)

        return None

    async def check_order_fill_status(self, order_id: str) -> FillResult:
        """Wait the configured grace period, then check the order: fully
        filled -> done; partially filled -> cancel the remainder and
        report the *actual* filled amount so the caller sizes the
        stop-loss and trade record off real fills, not the intended size;
        unfilled -> cancel."""
        await asyncio.sleep(self.fill_check_delay_s)
        try:
            order = await with_retries(self.exchange.fetch_order, order_id, self.symbol, logger=self.log)
        except ccxt.ExchangeError as e:
            self.log.error("Could not fetch order %s to check fill status: %s", order_id, e)
            raise

        status = order.get("status")
        filled = float(order.get("filled") or 0.0)

        if status == "closed":
            return FillResult(order_id, "closed", filled, order.get("average"))

        if filled > 0:
            self.log.warning(
                "Order %s partially filled (%.6f of %.6f) after %.0fs - canceling remainder.",
                order_id, filled, order.get("amount", filled), self.fill_check_delay_s,
            )
            await self._cancel_safely(order_id)
            return FillResult(order_id, "partial", filled, order.get("average"))

        self.log.warning("Order %s unfilled after %.0fs - canceling.", order_id, self.fill_check_delay_s)
        await self._cancel_safely(order_id)
        return FillResult(order_id, "canceled", 0.0, None)

    async def _cancel_safely(self, order_id: str) -> None:
        try:
            await with_retries(self.exchange.cancel_order, order_id, self.symbol, logger=self.log)
        except ccxt.ExchangeError as e:
            # May have filled in the instant between our status check and
            # this cancel request - not fatal, just log and move on.
            self.log.info("cancel_order for %s returned %s (may have just filled).", order_id, e)

    async def place_stop_loss(self, position_side: str, amount: float, stop_price: float) -> dict:
        """NOTE: stop-order parameter names/types vary by exchange and
        ccxt version (stopPrice vs triggerPrice, 'stop_market' vs 'stop').
        Verify the exact params your exchange expects on testnet."""
        close_side = "sell" if position_side == "long" else "buy"
        stop_price = float(self.exchange.price_to_precision(self.symbol, stop_price))
        amount = float(self.exchange.amount_to_precision(self.symbol, amount))
        params = {"stopPrice": stop_price, "reduceOnly": True}
        try:
            return await with_retries(
                self.exchange.create_order, self.symbol, "stop_market", close_side, amount, None, params,
                logger=self.log,
            )
        except ccxt.ExchangeError as e:
            self.log.error(
                "Failed to place exchange-side stop-loss (%s) - RiskManager's emergency "
                "check is now the ONLY protection on this position until this is retried.", e,
            )
            raise

    async def market_close_position(self, position_side: str, amount: float) -> dict:
        """Used only on the emergency hard-stop path: guarantee the fill
        and accept the slippage, rather than optimizing for price."""
        close_side = "sell" if position_side == "long" else "buy"
        amount = float(self.exchange.amount_to_precision(self.symbol, amount))
        params = {"reduceOnly": True}
        return await with_retries(
            self.exchange.create_order, self.symbol, "market", close_side, amount, None, params,
            logger=self.log,
        )
