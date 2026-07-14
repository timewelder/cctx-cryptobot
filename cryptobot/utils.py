"""
utils.py
--------
Shared, cross-cutting helpers used by every module: logging setup, an
async retry/backoff wrapper for transient exchange errors, and a helper
for aligning the main loop to candle-close boundaries.

Keeping these in one place avoids duplicating error-handling boilerplate
across DataManager, RiskManager, and ExecutionEngine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable, TypeVar

import ccxt  # only used for its exception classes here - no network calls happen in this file

T = TypeVar("T")

TIMEFRAME_TO_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440,
}


def setup_logging(log_path: str, level: int = logging.INFO) -> logging.Logger:
    """Configure a logger that writes to both console and a rotating file.

    Safe to call more than once (e.g. if re-imported during testing) -
    it won't duplicate handlers.
    """
    logger = logging.getLogger("trading_bot")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


async def with_retries(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    retries: int = 3,
    base_delay: float = 1.5,
    logger: logging.Logger | None = None,
    **kwargs: Any,
) -> T:
    """Call an async CCXT method, retrying transient errors with
    exponential backoff. Error classes are handled differently on
    purpose:

    - ccxt.RateLimitExceeded / ccxt.NetworkError: transient, worth
      retrying (covers RequestTimeout, ExchangeNotAvailable, DDoSProtection).
    - ccxt.ExchangeError (and subclasses not covered above): usually a
      permanent/logical problem (bad params, insufficient funds, invalid
      order) - retrying blindly would just fail the same way again, so
      these are re-raised immediately for the caller to handle.
    """
    log = logger or logging.getLogger("trading_bot")
    attempt = 0
    while True:
        try:
            return await func(*args, **kwargs)
        except ccxt.RateLimitExceeded as e:
            attempt += 1
            if attempt > retries:
                log.error("Rate limit exceeded, retries exhausted: %s", e)
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Rate limited (attempt %d/%d) - backing off %.1fs", attempt, retries, delay)
            await asyncio.sleep(delay)
        except ccxt.NetworkError as e:
            attempt += 1
            if attempt > retries:
                log.error("Network error, retries exhausted: %s", e)
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Network error (attempt %d/%d): %s - retrying in %.1fs", attempt, retries, e, delay)
            await asyncio.sleep(delay)
        except ccxt.ExchangeError as e:
            log.error("Exchange error (not retrying - likely permanent): %s", e)
            raise


def seconds_until_next_candle_close(timeframe: str, buffer_seconds: float = 5.0) -> float:
    """How long to sleep so the loop wakes up shortly after the next
    candle boundary (e.g. 15m -> :00, :15, :30, :45). A small buffer is
    added since exchanges can take a moment to finalize the just-closed
    candle.
    """
    minutes = TIMEFRAME_TO_MINUTES.get(timeframe)
    if minutes is None:
        raise ValueError(f"Unsupported timeframe for alignment: {timeframe!r}")

    interval_s = minutes * 60
    now = time.time()
    remainder = now % interval_s
    sleep_s = (interval_s - remainder) if remainder != 0 else 0.0
    return sleep_s + buffer_seconds
