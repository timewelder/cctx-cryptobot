"""
strategy_engine.py
-------------------
Pure, stateless indicator and signal computation. Given an OHLCV
DataFrame, StrategyEngine computes a standalone ATR(14), an RSI(14), and
a Supertrend(10, 3), then derives long/short entry booleans.

Every indicator here is causal: the value at row i is a function of
rows <= i only (rolling/EWM windows never reach forward). That means
there is no look-ahead bias baked into the math itself - the other half
of that guarantee is DataManager making sure the *last* row it hands to
this module is always a fully closed candle (see data_manager.py).

Note on the two ATRs: Supertrend conventionally uses its own internal
ATR, governed by its own `period` parameter (10 here) - this is a
different calculation from the standalone ATR(14) used for the
volatility filter and (via RiskManager) for stop-loss sizing. They are
kept deliberately separate rather than sharing one column, matching how
these indicators are used in practice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategyConfig:
    atr_period: int = 14
    rsi_period: int = 14
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    atr_volatility_threshold: float = 0.0


class StrategyEngine:
    def __init__(self, config: StrategyConfig, logger: logging.Logger | None = None):
        self.config = config
        self.log = logger or logging.getLogger("trading_bot")

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    def compute_atr(self, df: pd.DataFrame, period: int | None = None) -> pd.Series:
        """Wilder's ATR. True range at row 0 falls back to high-low since
        there's no previous close to compare against - standard practice."""
        period = period or self.config.atr_period
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1, skipna=True)
        atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        return atr.rename("atr")

    def compute_rsi(self, df: pd.DataFrame, period: int | None = None) -> pd.Series:
        """Wilder's RSI, with explicit handling of the degenerate cases
        (no losses at all -> 100; completely flat market -> 50) so the
        output never produces inf/NaN inside a valid warmup window."""
        period = period or self.config.rsi_period
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.where(avg_loss != 0, 100.0)
        rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
        return rsi.rename("rsi")

    def compute_supertrend(
        self, df: pd.DataFrame, period: int | None = None, multiplier: float | None = None
    ) -> pd.DataFrame:
        """Standard Supertrend: recursive final-band construction followed
        by a stateful flip rule. This part is inherently sequential (each
        row depends on the previous one), so it's computed with an
        explicit loop over numpy arrays rather than a vectorized pandas
        op - typical candle counts (hundreds to low thousands) make this
        fast enough with no need for extra dependencies.
        """
        period = period or self.config.supertrend_period
        multiplier = multiplier if multiplier is not None else self.config.supertrend_multiplier

        supertrend_atr = self.compute_atr(df, period=period)
        hl2 = (df["high"] + df["low"]) / 2
        basic_upper = (hl2 + multiplier * supertrend_atr).to_numpy()
        basic_lower = (hl2 - multiplier * supertrend_atr).to_numpy()
        close = df["close"].to_numpy()
        n = len(df)

        final_upper = np.full(n, np.nan)
        final_lower = np.full(n, np.nan)
        supertrend = np.full(n, np.nan)
        direction = np.zeros(n, dtype=int)  # 1 = uptrend, -1 = downtrend

        first_valid = supertrend_atr.first_valid_index()
        df_out = df.copy()
        if first_valid is None:
            df_out["supertrend"] = supertrend
            df_out["supertrend_direction"] = direction
            return df_out

        start = df.index.get_loc(first_valid)
        final_upper[start] = basic_upper[start]
        final_lower[start] = basic_lower[start]
        # Arbitrary initial direction - the recursion self-corrects within
        # a handful of bars, which is standard/accepted for this indicator.
        supertrend[start] = final_lower[start]
        direction[start] = 1

        for i in range(start + 1, n):
            if np.isnan(basic_upper[i]):
                continue

            final_upper[i] = (
                basic_upper[i]
                if (basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1])
                else final_upper[i - 1]
            )
            final_lower[i] = (
                basic_lower[i]
                if (basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1])
                else final_lower[i - 1]
            )

            if direction[i - 1] == 1:
                if close[i] < final_lower[i]:
                    direction[i] = -1
                    supertrend[i] = final_upper[i]
                else:
                    direction[i] = 1
                    supertrend[i] = final_lower[i]
            else:
                if close[i] > final_upper[i]:
                    direction[i] = 1
                    supertrend[i] = final_lower[i]
                else:
                    direction[i] = -1
                    supertrend[i] = final_upper[i]

        df_out["supertrend"] = supertrend
        df_out["supertrend_direction"] = direction
        return df_out

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attach supertrend / atr / rsi columns plus long_entry /
        short_entry booleans to a copy of the input DataFrame.

        Long entry:  close > supertrend AND RSI crosses above 30 AND ATR > threshold
        Short entry: close < supertrend AND RSI crosses below 70 AND ATR > threshold
        """
        out = self.compute_supertrend(df)
        out["atr"] = self.compute_atr(df, period=self.config.atr_period)
        out["rsi"] = self.compute_rsi(df)

        rsi_prev = out["rsi"].shift(1)
        crossed_above_30 = (rsi_prev <= 30) & (out["rsi"] > 30)
        crossed_below_70 = (rsi_prev >= 70) & (out["rsi"] < 70)
        atr_ok = out["atr"] > self.config.atr_volatility_threshold

        trend_up = out["close"] > out["supertrend"]
        trend_down = out["close"] < out["supertrend"]

        out["long_entry"] = (trend_up & crossed_above_30 & atr_ok).fillna(False)
        out["short_entry"] = (trend_down & crossed_below_70 & atr_ok).fillna(False)
        return out

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def check_indicator_correlation(self, df: pd.DataFrame) -> pd.DataFrame:
        """Quick diagnostic (not used by the trading logic itself) to
        confirm ATR, RSI, and Supertrend-distance aren't redundant with
        each other. Worth re-running if you change any indicator periods.
        """
        signals = self.generate_signals(df)
        diag = pd.DataFrame(
            {
                "atr": signals["atr"],
                "rsi": signals["rsi"],
                "supertrend_distance": (signals["close"] - signals["supertrend"]).abs(),
            }
        ).dropna()
        return diag.corr()
