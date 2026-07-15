"""
data_manager.py
----------------
Module 1: Data Layer & Persistence.

DataManager owns the CCXT exchange connection, fetches and cleans OHLCV
data (detecting and repairing gaps so no NaN rows reach the indicator
math), and persists trades / bot state / equity history / gap logs to a
local SQLite database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async
import pandas as pd

from config import Settings
from utils import TIMEFRAME_TO_MINUTES, with_retries


class DataManager:
    def __init__(self, settings: Settings, logger: logging.Logger | None = None):
        self.settings = settings
        self.log = logger or logging.getLogger("trading_bot")
        self.exchange = self._build_exchange()
        self.db_path = settings.db_path
        self._init_db()

    # ------------------------------------------------------------------
    # Exchange connection
    # ------------------------------------------------------------------

    def _build_exchange(self) -> ccxt_async.Exchange:
        exchange_class = getattr(ccxt_async, self.settings.exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unknown ccxt exchange id: {self.settings.exchange_id!r}")

        exchange = exchange_class({
            "apiKey": self.settings.api_key,
            "secret": self.settings.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "linear"},
        })
        exchange.enable_demo_trading(True)
        return exchange

    async def close(self) -> None:
        await self.exchange.close()

    async def load_markets(self) -> None:
        await with_retries(self.exchange.load_markets, logger=self.log)
        try:
            # Corrects exchange.milliseconds() for local clock drift, which
            # matters for the "is this candle actually closed yet" check
            # below. Not every exchange implements this - failure is fine.
            await with_retries(self.exchange.load_time_difference, logger=self.log)
        except Exception as e:  # noqa: BLE001
            self.log.debug("load_time_difference unavailable for %s: %s", self.settings.exchange_id, e)

    # ------------------------------------------------------------------
    # OHLCV fetch + gap handling
    # ------------------------------------------------------------------

    async def fetch_ohlcv(self) -> pd.DataFrame:
        try:
            raw = await with_retries(
                self.exchange.fetch_ohlcv,
                self.settings.symbol,
                timeframe=self.settings.timeframe,
                limit=self.settings.ohlcv_limit,
                logger=self.log,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            self.log.error("Failed to fetch OHLCV after retries: %s", e)
            raise

        if not raw:
            raise RuntimeError("Exchange returned no OHLCV data.")

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

        df = self._drop_unclosed_candle(df)
        df = self._handle_gaps(df)
        return df

    def _drop_unclosed_candle(self, df: pd.DataFrame) -> pd.DataFrame:
        """Some exchanges include the currently-forming candle as the last
        row. If its close time hasn't arrived yet, drop it so indicators
        never see a partial bar."""
        minutes = TIMEFRAME_TO_MINUTES[self.settings.timeframe]
        candle_duration = pd.Timedelta(minutes=minutes)
        now = pd.Timestamp(self.exchange.milliseconds(), unit="ms", tz="UTC")
        last_ts = df.iloc[-1]["timestamp"]
        if last_ts + candle_duration > now:
            self.log.debug("Dropping last candle at %s - not yet closed (now=%s).", last_ts, now)
            df = df.iloc[:-1].reset_index(drop=True)
        return df

    def detect_gaps(self, df: pd.DataFrame) -> list[pd.Timestamp]:
        """Return the timestamps of any missing candles implied by jumps
        in the timestamp column larger than one timeframe interval."""
        minutes = TIMEFRAME_TO_MINUTES[self.settings.timeframe]
        expected_delta = pd.Timedelta(minutes=minutes)
        deltas = df["timestamp"].diff().dropna()
        gap_row_indices = deltas[deltas > expected_delta].index

        gaps: list[pd.Timestamp] = []
        for idx in gap_row_indices:
            prev_ts = df.loc[idx - 1, "timestamp"]
            n_missing = int(deltas[idx] / expected_delta) - 1
            for m in range(1, n_missing + 1):
                gaps.append(prev_ts + m * expected_delta)
        return gaps

    def _handle_gaps(self, df: pd.DataFrame) -> pd.DataFrame:
        """Forward-fill missing candles (close carried forward, O/H/L set
        to that close, volume 0) rather than letting NaN rows reach the
        indicator math, and log every gap for later review."""
        gaps = self.detect_gaps(df)
        if not gaps:
            return df

        self.log.warning("Detected %d missing candle(s): %s%s", len(gaps), gaps[:5], " ..." if len(gaps) > 5 else "")
        for ts in gaps:
            self._log_gap(ts)

        minutes = TIMEFRAME_TO_MINUTES[self.settings.timeframe]
        full_index = pd.date_range(df["timestamp"].min(), df["timestamp"].max(), freq=f"{minutes}min")
        df = df.set_index("timestamp").reindex(full_index)
        df.index.name = "timestamp"

        df["close"] = df["close"].ffill()
        for col in ("open", "high", "low"):
            df[col] = df[col].fillna(df["close"])
        df["volume"] = df["volume"].fillna(0.0)

        return df.reset_index()

    def _log_gap(self, ts: pd.Timestamp) -> None:
        conn = self._connect_db()
        try:
            conn.execute(
                "INSERT INTO ohlcv_gaps (symbol, timeframe, missing_timestamp, detected_at) VALUES (?, ?, ?, ?)",
                (self.settings.symbol, self.settings.timeframe, ts.isoformat(), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # SQLite persistence
    # ------------------------------------------------------------------

    def _connect_db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        conn = self._connect_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    size_base REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    stop_price REAL,
                    pnl REAL,
                    status TEXT NOT NULL DEFAULT 'open'
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ohlcv_gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    missing_timestamp TEXT NOT NULL,
                    detected_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_open_trade(self, side: str, entry_price: float, size_base: float, notional_usd: float, stop_price: float) -> int:
        conn = self._connect_db()
        try:
            cur = conn.execute(
                """INSERT INTO trades (opened_at, symbol, side, entry_price, size_base, notional_usd, stop_price, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
                (datetime.now(timezone.utc).isoformat(), self.settings.symbol, side, entry_price, size_base, notional_usd, stop_price),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def close_trade(self, trade_id: int, exit_price: float, pnl: float) -> None:
        conn = self._connect_db()
        try:
            conn.execute(
                "UPDATE trades SET closed_at = ?, exit_price = ?, pnl = ?, status = 'closed' WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), exit_price, pnl, trade_id),
            )
            conn.commit()
        finally:
            conn.close()

    def record_equity(self, equity: float) -> None:
        conn = self._connect_db()
        try:
            conn.execute(
                "INSERT INTO equity_history (timestamp, equity) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), equity),
            )
            conn.commit()
        finally:
            conn.close()

    def save_state(self, key: str, value: Any) -> None:
        conn = self._connect_db()
        try:
            conn.execute(
                """INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, json.dumps(value), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def load_state(self, key: str, default: Any = None) -> Any:
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        finally:
            conn.close()

    def get_performance_summary(self) -> dict:
        conn = self._connect_db()
        try:
            trades = pd.read_sql_query("SELECT * FROM trades WHERE status = 'closed'", conn)
            equity = pd.read_sql_query("SELECT * FROM equity_history ORDER BY timestamp", conn)
        finally:
            conn.close()

        if trades.empty:
            realized_pnl, wins, losses, win_loss_ratio = 0.0, 0, 0, float("nan")
        else:
            realized_pnl = float(trades["pnl"].sum())
            wins = int((trades["pnl"] > 0).sum())
            losses = int((trades["pnl"] < 0).sum())
            win_loss_ratio = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else float("nan"))

        if equity.empty:
            max_drawdown_pct = 0.0
        else:
            running_max = equity["equity"].cummax()
            drawdown = (equity["equity"] - running_max) / running_max
            max_drawdown_pct = float(drawdown.min() * 100)

        return {
            "realized_pnl": realized_pnl,
            "wins": wins,
            "losses": losses,
            "win_loss_ratio": win_loss_ratio,
            "max_drawdown_pct": max_drawdown_pct,
            "total_closed_trades": int(len(trades)),
        }
