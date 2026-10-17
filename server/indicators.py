"""One numeric implementation for the chart and future AI snapshots.

EMA seeds with an SMA; RSI uses Wilder smoothing; MACD histogram is DIF-DEA.
Gaps and unconfirmed past bars reset the calculation instead of bridging unknown data.
"""
from .models import Candle, TIMEFRAMES

VERSION = "indicators-v1"


def ema(values: list[float | None], period: int) -> list[float | None]:
    out: list[float | None] = []
    seed: list[float] = []
    current = None
    alpha = 2 / (period + 1)
    for value in values:
        if value is None:
            seed, current = [], None
        elif current is None:
            seed.append(value)
            if len(seed) == period:
                current = sum(seed) / period
        else:
            current += alpha * (value - current)
        out.append(current)
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    gain = loss = 0.0
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        up, down = max(delta, 0), max(-delta, 0)
        if i <= period:
            gain += up / period
            loss += down / period
        else:
            gain = (gain * (period - 1) + up) / period
            loss = (loss * (period - 1) + down) / period
        if i >= period:
            out[i] = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def _segment(bars: list[Candle]) -> list[dict]:
    closes = [b.close for b in bars]
    e20, e60 = ema(closes, 20), ema(closes, 60)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    strength = rsi(closes)
    return [{"time": bar.time, "ema20": e20[i], "ema60": e60[i], "rsi14": strength[i],
             "macd": dif[i], "signal": dea[i],
             "histogram": dif[i] - dea[i] if dif[i] is not None and dea[i] is not None else None,
             "provisional": bar.status != "confirmed", "samples": i + 1}
            for i, bar in enumerate(bars)]


def calculate(bars: list[Candle], adjacent=None) -> list[dict]:
    if not bars:
        return []
    result, segment = [], []
    for bar in bars:
        # An old forming bar must never silently enter a confirmed series.
        contiguous = not segment or (adjacent(segment[-1], bar) if adjacent else bar.time == segment[-1].end)
        if segment and (not contiguous or segment[-1].status != "confirmed"):
            result.extend(_segment(segment))
            segment = []
        segment.append(bar)
    result.extend(_segment(segment))
    return result
