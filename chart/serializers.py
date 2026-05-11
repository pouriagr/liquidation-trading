"""Serialize DB rows into the JSON shapes Lightweight Charts expects.

Lightweight Charts wants `{ time, ... }` where `time` is a UTC unix
timestamp in seconds. All of our tz-aware UTC datetime columns convert
with a plain `int(dt.timestamp())` — no `astimezone()` call, the chart
converts to local at the time-axis layer.

Note: numeric values are emitted as `float` here because Lightweight
Charts only accepts numbers and IEEE-754 doubles are lossless at
Binance's reported precision (≤7 integer digits, 8 decimal places).
Anything that needs exact Decimal arithmetic (PnL, backtests) must
read the ORM models directly — do not route precision-sensitive math
through this JSON layer.
"""

from collections.abc import Iterable

from data.models import Candle

# Funding histogram colours match the candle volume conventions:
# green when positive (longs pay), red when negative (shorts pay).
_FUNDING_UP = "#26a69a"
_FUNDING_DOWN = "#ef5350"


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


# ---- indicator payloads ----------------------------------------------------
# Three shared-shape envelopes used by the sub-pane endpoints: each one
# is `{ symbol, [period|interval,] count, points: [...] }` where each
# point is a Lightweight Charts data point (`{time, value, ...}`). The
# pane reads `points` directly into a series via `.setData(points)`.


def oi_payload(symbol: str, period: str, rows: Iterable[dict]) -> dict:
    """OpenInterest rows → Lightweight Charts line-series points.

    `value` is the USD notional (`sum_open_interest_value`). It's the
    column the framework references for cluster-strength analysis and
    has a friendlier visual scale than the base-coin amount.
    """
    points = [
        {
            "time": int(r["timestamp"].timestamp()),
            "value": float(r["sum_open_interest_value"]),
        }
        for r in rows
    ]
    return {
        "symbol": symbol,
        "period": period,
        "count": len(points),
        "points": points,
    }


def funding_payload(symbol: str, rows: Iterable[dict]) -> dict:
    """FundingRate rows → Lightweight Charts histogram-series points.

    Per-point colour is set here (not in the JS) so the serializer is
    the single source of "what does positive vs negative funding look
    like" — keeps the convention consistent with `candle_to_dict`'s
    silence on colours (handled there by candle-up vs candle-down at
    render time, here by funding-sign at serialize time because the
    histogram series doesn't get an OHLC-derived colour rule).
    """
    points = []
    for r in rows:
        rate = r["funding_rate"]
        points.append(
            {
                "time": int(r["funding_time"].timestamp()),
                "value": float(rate),
                "color": _FUNDING_UP if rate >= 0 else _FUNDING_DOWN,
            }
        )
    return {"symbol": symbol, "count": len(points), "points": points}


def cvd_payload(symbol: str, interval: str, series: Iterable[tuple]) -> dict:
    """CVD `(datetime, Decimal | None)` series → line-series points.

    A `None` window (gap or insufficient history) becomes a whitespace
    point — `{"time": ...}` with no `value` key. Lightweight Charts
    treats those as gaps and breaks the line, which matches the
    framework's "CVD is undefined when its inputs are partial" rule
    (`docs/liquidation_framework_concept.md` §3.4).
    """
    points: list[dict] = []
    for ts, value in series:
        ts_int = int(ts.timestamp())
        if value is None:
            points.append({"time": ts_int})
        else:
            points.append({"time": ts_int, "value": float(value)})
    return {
        "symbol": symbol,
        "interval": interval,
        "count": len(points),
        "points": points,
    }
