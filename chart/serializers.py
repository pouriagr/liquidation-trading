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
from decimal import Decimal

from data.models import Candle
from feature.controllers.cluster_identifier import ClusterIdentifierController

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


def cluster_payload(
    symbol: str,
    rows: Iterable[dict],
    *,
    selected_windows: list[int] | None = None,
) -> dict:
    """Persisted `ClusterSegment` rows → JSON envelope for the chart overlay.

    Reads ORM rows materialised via `.values(...)` (cheaper than full
    model instantiation; we don't need methods on the rows). The view
    is expected to pass them ordered ascending by `start_time` and to
    have already filtered by `lookback_hours__in=selected_windows`.

    When `selected_windows` contains more than one window the rows
    are merged here per `(price_band, side)` — the §12.3
    multi-resolution confluence boost, realised as the §5.4
    sum-of-contributions rule applied along the window axis (the
    dict-space mirror of the service-layer
    `aggregate_segments_across_windows` helper, which exists for the
    backtest harness's dataclass path). Merge rules:

      * strength      → sum across windows
      * notional      → sum across windows
      * long_bias     → notional-weighted average
      * start_time / source_time → min across windows
      * end_time      → min of non-None values; null only if every
        contributing row was alive
      * lookback_hours → sorted list of windows that contributed
        (scalar for the single-window path) — useful for a future
        tooltip "confirmed in 24h + 72h"

    `generated_at` on the envelope is the max across all returned
    rows — represents "when the most recent refresh wrote this
    symbol's clusters". `null` when the symbol hasn't been refreshed.

    `anchor_price` is the median of segment prices, computed AFTER
    any merge so it reflects the rendered set. The heatmap aggregator
    in `home.js` uses it to convert between price space and the
    `priceBin` axis of its 2D cell grid. Median (rather than the
    latest-close used at compute time) is symbol-agnostic, stable
    across requests, and works correctly even when the symbol has
    swung ±50% since the source anchor was captured — bins land on
    the same prices either way.

    The envelope's `windows` field carries `selected` (what was
    requested / served) and `available` (the full
    `SUPPORTED_LOOKBACKS` tuple), so the client can render the pill
    state without a separate config endpoint.
    """
    rows = list(rows)
    available = list(ClusterIdentifierController.SUPPORTED_LOOKBACKS)
    selected = list(selected_windows) if selected_windows is not None else available

    merged_rows: list[dict]
    if len(selected) > 1 and rows:
        merged_rows = _merge_cluster_rows(rows)
    else:
        # Single-window path: keep `lookback_hours` as a scalar.
        merged_rows = rows

    generated_at = max((r["generated_at"] for r in rows), default=None)
    anchor: float
    if merged_rows:
        prices_sorted = sorted(float(r["price"]) for r in merged_rows)
        anchor = prices_sorted[len(prices_sorted) // 2]
    else:
        anchor = 0.0
    return {
        "symbol": symbol,
        "generated_at": int(generated_at.timestamp()) if generated_at is not None else None,
        "anchor_price": anchor,
        "windows": {
            "selected": selected,
            "available": available,
        },
        "segments": [_segment_to_dict(r) for r in merged_rows],
    }


def _segment_to_dict(r: dict) -> dict:
    """One row → wire dict. Handles both raw single-window rows (where
    `lookback_hours` is the scalar from the DB) and merged multi-window
    rows (where `lookback_hours` is a sorted list of contributors)."""
    return {
        "price_low": float(r["price_low"]),
        "price_high": float(r["price_high"]),
        "price": float(r["price"]),
        "side": r["side"],
        "start_time": int(r["start_time"].timestamp()),
        "end_time": (int(r["end_time"].timestamp()) if r["end_time"] is not None else None),
        "strength": r["strength"],
        "notional": float(r["notional"]),
        "long_bias": r["long_bias"],
        "source_time": int(r["source_open_time"].timestamp()),
        "lookback_hours": r["lookback_hours"],
    }


def _merge_cluster_rows(rows: list[dict]) -> list[dict]:
    """Group by `(price_low, price_high, side)`; apply §12.3 confluence merge.

    Dict-space mirror of
    `feature.services.clustering.aggregate_segments_across_windows`.
    Lives here rather than going through the dataclass helper because
    the view supplies `.values(...)` dicts already and round-tripping
    through `ClusterSegment` instances would be pure overhead. The two
    implementations share the same merge rules and are covered by
    parallel tests.
    """
    # 8-dp quantize defends against any final-step Decimal wisp from the
    # geometric-grid arithmetic — same rationale as the service-layer
    # helper. The grouping key includes `source_open_time`: same band at
    # different anchors must stay distinct so the §5 reactivation
    # pattern survives the merge (a level activated 5 times over a
    # year is 5 segments, not one rectangle anchored at its earliest
    # appearance). Confluence falls out per anchor: if three windows
    # all qualified the same accumulation hour at the same band, the
    # merge sums their three strengths into one segment.
    eight_dp = Decimal("0.00000001")
    bucket: dict[tuple[Decimal, Decimal, str, object], dict] = {}
    for r in rows:
        key = (
            Decimal(r["price_low"]).quantize(eight_dp),
            Decimal(r["price_high"]).quantize(eight_dp),
            r["side"],
            r["source_open_time"],
        )
        notional = Decimal(r["notional"])
        bias_w = float(r["long_bias"]) * float(notional)
        slot = bucket.get(key)
        if slot is None:
            bucket[key] = {
                "price_low": r["price_low"],
                "price_high": r["price_high"],
                "price": r["price"],
                "side": r["side"],
                "strength": float(r["strength"]),
                "notional": notional,
                "bias_weighted": bias_w,
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "source_open_time": r["source_open_time"],
                "generated_at": r["generated_at"],
                "lookback_hours": [int(r["lookback_hours"])],
            }
            continue
        slot["strength"] += float(r["strength"])
        slot["notional"] += notional
        slot["bias_weighted"] += bias_w
        # `start_time` and `source_open_time` are identical within a
        # group by construction. `end_time` is defensive-min across
        # windows in case the same band's sweep ever resolves
        # differently (today: same 5m tape ⇒ same answer).
        if r["end_time"] is not None:
            if slot["end_time"] is None or r["end_time"] < slot["end_time"]:
                slot["end_time"] = r["end_time"]
        slot["lookback_hours"].append(int(r["lookback_hours"]))

    out: list[dict] = []
    for slot in bucket.values():
        notional = slot["notional"]
        long_bias = slot["bias_weighted"] / float(notional) if notional > 0 else 0.0
        out.append(
            {
                "price_low": slot["price_low"],
                "price_high": slot["price_high"],
                "price": slot["price"],
                "side": slot["side"],
                "strength": slot["strength"],
                "notional": notional,
                "long_bias": long_bias,
                "start_time": slot["start_time"],
                "end_time": slot["end_time"],
                "source_open_time": slot["source_open_time"],
                "generated_at": slot["generated_at"],
                "lookback_hours": sorted(set(slot["lookback_hours"])),
            }
        )
    out.sort(key=lambda r: (r["start_time"], r["price_low"], r["side"]))
    return out


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
