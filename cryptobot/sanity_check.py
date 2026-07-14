"""
sanity_check.py
----------------
Standalone verification - NO network calls, NO API keys required.
Generates synthetic OHLCV data and exercises StrategyEngine's indicator
math and DataManager's gap-detection/fill logic, so you can confirm
your environment and the core calculations behave sanely before ever
pointing this at a real exchange.

Run: python sanity_check.py
"""

from __future__ import annotations

import logging
import os
import tempfile

import numpy as np
import pandas as pd

from strategy_engine import StrategyConfig, StrategyEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("sanity_check")


def make_synthetic_ohlcv(n: int = 300, start_price: float = 30_000.0, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = rng.normal(loc=0.0, scale=0.002, size=n)
    close = start_price * np.cumprod(1 + returns)
    open_ = np.concatenate([[start_price], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.001, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.001, n))
    volume = rng.uniform(10, 100, n)
    timestamps = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "timestamp": timestamps, "open": open_, "high": high,
        "low": low, "close": close, "volume": volume,
    })


def check_strategy_engine() -> None:
    df = make_synthetic_ohlcv()
    engine = StrategyEngine(StrategyConfig())
    signals = engine.generate_signals(df)

    warmed_up = signals.iloc[30:]  # skip indicator warmup
    assert warmed_up["rsi"].between(0, 100).all(), "RSI escaped [0, 100]"
    assert (warmed_up["atr"] >= 0).all(), "ATR went negative"
    assert warmed_up["supertrend"].notna().all(), "Supertrend has NaNs after warmup"
    assert signals["long_entry"].dtype == bool
    assert signals["short_entry"].dtype == bool
    assert not (signals["long_entry"] & signals["short_entry"]).any(), "Long and short fired on the same candle"

    n_long = int(signals["long_entry"].sum())
    n_short = int(signals["short_entry"].sum())
    corr = engine.check_indicator_correlation(df)

    log.info("StrategyEngine OK - %d long signal(s), %d short signal(s) over %d candles.", n_long, n_short, len(df))
    log.info("Indicator correlation matrix:\n%s", corr.round(3))


def check_gap_handling() -> None:
    # Imported here (not at module level) so this script can still run
    # check_strategy_engine() even before `pip install -r requirements.txt`
    # has pulled in ccxt/python-dotenv.
    os.environ.setdefault("API_KEY", "dummy")
    os.environ.setdefault("API_SECRET", "dummy")

    from config import Settings
    from data_manager import DataManager

    df = make_synthetic_ohlcv(n=50)
    gapped = pd.concat([df.iloc[:20], df.iloc[23:]]).reset_index(drop=True)  # drop 3 candles

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            exchange_id="binanceusdm", api_key="dummy", api_secret="dummy", testnet=True,
            symbol="BTC/USDT:USDT", timeframe="15m",
            db_path=os.path.join(tmp, "test.db"), log_path=os.path.join(tmp, "test.log"),
        )
        dm = DataManager(settings)

        gaps = dm.detect_gaps(gapped)
        assert len(gaps) == 3, f"Expected 3 missing candles, found {len(gaps)}"

        filled = dm._handle_gaps(gapped)
        assert len(filled) == len(df), f"Expected {len(df)} rows after fill, got {len(filled)}"
        assert filled["close"].notna().all()
        assert filled["volume"].notna().all()

        dm.save_state("test_key", {"hello": "world"})
        assert dm.load_state("test_key") == {"hello": "world"}

        log.info("DataManager gap detection + fill + SQLite state OK - %d gap(s) found and repaired.", len(gaps))


if __name__ == "__main__":
    check_strategy_engine()
    try:
        check_gap_handling()
    except ModuleNotFoundError as e:
        log.warning("Skipping DataManager check - install requirements.txt first (%s).", e)
    log.info("Sanity checks complete.")
