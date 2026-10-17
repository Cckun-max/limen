from dataclasses import asdict, dataclass
import math
import time

# Keep the exchange's native UTC boundaries, including epoch-aligned (Thursday) weeks.
TIMEFRAMES = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000, "1w": 604_800_000,
}
INSTRUMENT = "hyperliquid:perp:BTC"
CONFIRM_DELAY_MS = 2_000


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class Candle:
    timeframe: str
    time: int
    end: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int
    status: str
    received_at: int
    revision: int = 1
    source: str = "rest"
    symbol: str = "BTC"

    def public(self) -> dict:
        return asdict(self)


def parse_candle(raw: dict, timeframe: str, received_at: int, *, source: str,
                 requested_at: int | None = None, symbol: str = "BTC") -> Candle:
    step = TIMEFRAMES[timeframe]
    start = int(raw["t"])
    if raw.get("s", symbol) != symbol or raw.get("i", timeframe) != timeframe:
        raise ValueError("Unexpected candle instrument or timeframe")
    if start % step != 0 or int(raw["T"]) not in (start + step - 1, start + step):
        raise ValueError("Invalid candle time bounds")
    if start > received_at + 5_000:
        raise ValueError("Candle starts in the future; check local clock")
    o, h, l, c, v = (float(raw[k]) for k in ("o", "h", "l", "c", "v"))
    if not all(math.isfinite(x) for x in (o, h, l, c, v)):
        raise ValueError("Non-finite candle value")
    if min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c):
        raise ValueError("Invalid OHLCV")
    closed = source == "rest" and start + step <= (requested_at or 0) - CONFIRM_DELAY_MS
    trades = int(raw.get("n", 0))
    if trades < 0:
        raise ValueError("Negative trade count")
    return Candle(timeframe, start, start + step, o, h, l, c, v,
                  trades, "confirmed" if closed else "forming",
                  received_at, source=source, symbol=symbol)


def missing_ranges(bars: list[Candle], timeframe: str, at: int) -> list[dict]:
    """Only report absent bars. A present but unconfirmed bar is tracked separately."""
    if not bars:
        return []
    step = TIMEFRAMES[timeframe]
    gaps = []
    for left, right in zip(bars, bars[1:]):
        if right.time > left.time + step:
            gaps.append({"start": left.time + step, "end": right.time,
                         "bars": (right.time - left.time) // step - 1})
    expected_last = ((at - CONFIRM_DELAY_MS) // step - 1) * step
    if bars[-1].time < expected_last:
        gaps.append({"start": bars[-1].time + step, "end": expected_last + step,
                     "bars": (expected_last - bars[-1].time) // step})
    return gaps
