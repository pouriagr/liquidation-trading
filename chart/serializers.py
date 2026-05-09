"""Serialize `Candle` rows into the JSON shape Lightweight Charts expects.

Lightweight Charts wants `{ time, open, high, low, close }` where `time` is a
UTC unix timestamp in seconds. Candle.open_time is tz-aware UTC, so the
conversion is just `int(open_time.timestamp())` — no astimezone() call, the
chart converts to local at the time-axis layer.

Note: OHLC + volume are emitted as `float` here because Lightweight Charts only
accepts numbers and IEEE-754 doubles are lossless at Binance's reported
precision (≤7 integer digits, 8 decimal places). Anything that needs exact
Decimal arithmetic (PnL, backtests) must read `Candle` directly — do not route
precision-sensitive math through this JSON layer.
"""

from collections.abc import Iterable

from data.models import Candle


def candle_to_dict(c: Candle) -> dict:
    """Map one Candle to the dict shape consumed by the chart's JS."""
    return {
        "time": int(c.open_time.timestamp()),
        "open": float(c.open),
        "high": float(c.high),
        "low": float(c.low),
        "close": float(c.close),
        "volume": float(c.volume),
    }


def candles_payload(
    symbol: str,
    interval: str,
    candles: Iterable[Candle],
    *,
    fetched: bool,
) -> dict:
    """Wrap the candle list with metadata for the API response.

    `candles` must already be ordered ascending by `open_time` — Lightweight
    Charts throws if the series isn't strictly ascending.
    """
    rows = [candle_to_dict(c) for c in candles]
    return {
        "symbol": symbol,
        "interval": interval,
        "count": len(rows),
        "fetched": fetched,
        "candles": rows,
    }
