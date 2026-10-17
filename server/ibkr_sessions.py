"""IBKR's own trading schedule is the authority; never invent holiday candles."""
from bisect import bisect_right
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from .models import Candle, TIMEFRAMES


def timestamp(value, zone="UTC"):
    if isinstance(value, str):
        value = datetime.strptime(value.replace("-", " "), "%Y%m%d %H:%M:%S")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(zone))
    return int(value.timestamp() * 1000)


class Sessions:
    def __init__(self, payload):
        self.zone = payload["zone"]
        self.start, self.end = payload["start"], payload["end"]
        self.rows = sorted(payload["rows"], key=lambda r: r["start"])
        self.starts = [r["start"] for r in self.rows]
        self.by_date = {}
        for row in self.rows:
            old = self.by_date.get(row["date"])
            self.by_date[row["date"]] = {**row, "start": min(old["start"],row["start"]) if old else row["start"],
                                         "end": max(old["end"],row["end"]) if old else row["end"]}

    def at(self, at):
        i = bisect_right(self.starts, at) - 1
        return self.rows[i] if i >= 0 and at < self.rows[i]["end"] else None

    def has_trading(self, start, end):
        if end <= start:
            return False
        i = max(0, bisect_right(self.starts, start) - 1)
        while i < len(self.rows) and self.rows[i]["start"] < end:
            if self.rows[i]["end"] > start:
                return True
            i += 1
        return False

    def adjacent(self, left, right):
        if left.end > right.time:
            return False
        if left.end == right.time:
            return True
        return self.start <= left.end and right.time <= self.end and not self.has_trading(left.end, right.time)

    def gaps(self, bars, timeframe, cutoff):
        gaps = [{"start": a.end, "end": b.time, "bars": max(1, (b.time-a.end)//TIMEFRAMES[timeframe])}
                for a, b in zip(bars, bars[1:]) if not self.adjacent(a, b)]
        # Only a whole missing interval during scheduled trading is a stale tail.
        if bars and timeframe not in {"1d", "1w"}:
            end = min(cutoff - TIMEFRAMES[timeframe], self.end)
            if self.has_trading(bars[-1].end, end):
                gaps.append({"start": bars[-1].end, "end": end, "bars": 1})
        if bars and timeframe == "1d":
            absent = [s for s in self.by_date.values() if s["start"] > bars[-1].time and s["end"] <= cutoff-2000]
            if absent:
                gaps.append({"start":absent[0]["start"],"end":absent[-1]["end"],"bars":len(absent)})
        return gaps


def normalize_bars(raw, timeframe, symbol, sessions, cutoff, received):
    bars = []
    for item in raw:
        if timeframe == "1d":
            d = item.date.date() if isinstance(item.date, datetime) else item.date
            session = sessions.by_date.get(d.strftime("%Y%m%d"))
            if session is None:
                raise ValueError("K 线交易日不在已验证的 IBKR 日历中")
            start, end = session["start"], session["end"]
        else:
            start = timestamp(item.date, sessions.zone)
            session = sessions.at(start)
            if session is None:
                raise ValueError("K 线时间不在 IBKR 交易时段中")
            end = min(start + TIMEFRAMES[timeframe], session["end"])
        if start > cutoff:
            raise ValueError("历史 K 线超出本次数据截止时间")
        values = [float(getattr(item, k)) for k in ("open", "high", "low", "close", "volume")]
        o, h, l, c, v = values
        if not all(math.isfinite(x) for x in values) or min(o,h,l,c) <= 0 or v < 0 or l > min(o,c) or h < max(o,c):
            raise ValueError("IBKR 返回无效 OHLCV，未写入缓存")
        bars.append(Candle(timeframe, start, end, o, h, l, c, v, max(0, int(item.barCount)),
                           "confirmed" if end <= cutoff - 2000 else "forming", received,
                           source="ibkr_history", symbol=symbol))
    bars.sort(key=lambda b: b.time)
    if len({b.time for b in bars}) != len(bars):
        raise ValueError("IBKR 返回重复 K 线时间")
    # IB may emit a shorter opening bar before its usual hour boundary.
    for a, b in zip(bars, bars[1:]):
        if b.time < a.end:
            a.end = b.time
            a.status = "confirmed" if a.end <= cutoff - 2000 else "forming"
    return bars


def weekly_bars(daily, sessions, cutoff):
    groups = {}
    for b in daily:
        session = sessions.at(b.time)
        d = datetime.strptime(session["date"], "%Y%m%d").date()
        monday = d - timedelta(days=d.weekday())
        groups.setdefault(monday, []).append(b)
    result = []
    for monday, bars in sorted(groups.items()):
        # Drop an incomplete leading week; no invented historical weekly candle.
        known = [s for s in sessions.by_date.values() if monday <= datetime.strptime(s["date"], "%Y%m%d").date() < monday + timedelta(days=7)]
        if not known or known[0]["start"] != bars[0].time:
            continue
        next_week = timestamp(datetime.combine(monday+timedelta(days=7), datetime.min.time()), sessions.zone)
        complete = (next_week <= cutoff and len(bars) == len(known)
                    and all(b.status == "confirmed" for b in bars)
                    and all(sessions.adjacent(a,b) for a,b in zip(bars,bars[1:])))
        first, last = bars[0], bars[-1]
        result.append(Candle("1w", first.time, last.end if complete else max(last.end, next_week),
                             first.open, max(b.high for b in bars), min(b.low for b in bars), last.close,
                             sum(b.volume for b in bars), sum(b.trades for b in bars),
                             "confirmed" if complete else "forming", last.received_at,
                             source="ibkr_history", symbol=first.symbol))
    return result
