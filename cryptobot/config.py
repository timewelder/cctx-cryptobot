"""
config.py
---------
Centralised configuration loader. Every tunable parameter comes from
environment variables (loaded from a local .env via python-dotenv) so
no secrets or exchange-specific settings are hardcoded into the source.

Copy `.env.example` to `.env` and fill in your own values before running.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val is not None else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val is not None else default


@dataclass(frozen=True)
class Settings:
    # --- Exchange / connectivity ---
    exchange_id: str = field(default_factory=lambda: os.getenv("EXCHANGE_ID", "binanceusdm"))
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("API_SECRET", ""))
    testnet: bool = field(default_factory=lambda: _get_bool("TESTNET", True))

    # --- Market ---
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "BTC/USDT:USDT"))
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "15m"))
    ohlcv_limit: int = field(default_factory=lambda: _get_int("OHLCV_LIMIT", 500))

    # --- Strategy ---
    atr_period: int = field(default_factory=lambda: _get_int("ATR_PERIOD", 14))
    rsi_period: int = field(default_factory=lambda: _get_int("RSI_PERIOD", 14))
    supertrend_period: int = field(default_factory=lambda: _get_int("SUPERTREND_PERIOD", 10))
    supertrend_multiplier: float = field(default_factory=lambda: _get_float("SUPERTREND_MULTIPLIER", 3.0))
    atr_volatility_threshold: float = field(default_factory=lambda: _get_float("ATR_VOLATILITY_THRESHOLD", 0.0))

    # --- Risk ---
    leverage: int = field(default_factory=lambda: _get_int("LEVERAGE", 2))
    risk_per_trade: float = field(default_factory=lambda: _get_float("RISK_PER_TRADE", 0.01))
    margin_safety_buffer: float = field(default_factory=lambda: _get_float("MARGIN_SAFETY_BUFFER", 0.95))

    # --- Execution ---
    limit_order_offset_pct: float = field(default_factory=lambda: _get_float("LIMIT_ORDER_OFFSET_PCT", 0.0005))
    fill_check_delay_s: float = field(default_factory=lambda: _get_float("FILL_CHECK_DELAY_S", 30.0))

    # --- Persistence / logging ---
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "trading_bot.db"))
    log_path: str = field(default_factory=lambda: os.getenv("LOG_PATH", "trading_bot.log"))

    def validate(self) -> None:
        errors = []
        if not self.api_key or not self.api_secret:
            errors.append("API_KEY / API_SECRET are not set (check your .env file).")
        if self.leverage not in (2, 3):
            errors.append(f"LEVERAGE={self.leverage} - spec calls for 2x or 3x only.")
        if not (0 < self.risk_per_trade < 0.1):
            errors.append(f"RISK_PER_TRADE={self.risk_per_trade} looks out of a sane range (0-10%).")
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))


settings = Settings()
