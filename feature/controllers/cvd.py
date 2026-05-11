"""On-demand CVD computed from `data.Candle.delta` over a 200-bar window.

Cumulative Volume Delta is *the running sum of `delta` over a fixed-size
trailing window of candles*. Per `docs/liquidation_framework_concept.md`
§3.4 / §4.3, only relative changes matter for divergence and absorption
analysis, so the absolute anchor is irrelevant — what matters is that
the same window length is used consistently. We use **200 bars**.

Nothing is persisted: every call recomputes from the latest `Candle`
rows. The model `Candle.delta` is already populated by a `pre_save`
signal in this app (see `feature.signals`), so the read path here is a
single ORM query plus a Python sum.

If the 200-bar window is incomplete — fewer than 200 candles exist for
the pair, any of them has `delta=NULL`, or the time grid has a gap —
the value returned is `None`. That convention matches the framework's
expectation that CVD is undefined when its inputs are partial, and lets
the chart layer render a gap instead of a misleading partial sum.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from data.models import Candle, Symbol

logger = logging.getLogger(__name__)


class CVDController:
    """Windowed CVD reader: sum of `Candle.delta` over the last 200 bars.

    Two entry points:
      * `latest(symbol, interval)` — one value for "now".
      * `series(symbol, interval, n)` — `n` values for charting, each
        computed over its own 200-bar trailing window.
    """

    # Fixed window per the spec — 200 of *the* most recent candles.
    WINDOW = 200

    # Cadence per allowed interval, used for the gap-detection check.
    # Mirrors `data.controllers.binance_klines_archive._INTERVAL_MINUTES`
    # but is duplicated here intentionally — depending on a `data`
    # controller's private constant would be the wrong direction of
    # coupling. `1M` is omitted: months are not uniform, so a span-based
    # contiguity check doesn't apply.
    _INTERVAL_MINUTES: dict[str, int] = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "2h": 120,
        "4h": 240,
        "6h": 360,
        "8h": 480,
        "12h": 720,
        "1d": 1440,
        "3d": 4320,
        "1w": 10080,
    }
    ALLOWED_SYMBOLS = frozenset(Symbol.values)
    ALLOWED_INTERVALS = frozenset(_INTERVAL_MINUTES)

    # ---- public entry points ----------------------------------------------
    def latest(self, symbol: str, interval: str) -> Decimal | None:
        """CVD over the 200 most recent candles, or `None` if incomplete.

        Returns `None` if any of these is true:
          * fewer than 200 candles exist for (symbol, interval),
          * the 200 newest candles have a time-grid gap,
          * any of those 200 rows has `delta=NULL`.
        """
        symbol, interval = self._validate(symbol, interval)
        window = self._fetch_window(symbol, interval, total=self.WINDOW)
        return self._sum_or_none(window, interval)

    def series(self, symbol: str, interval: str, n: int) -> list[tuple[datetime, Decimal | None]]:
        """The most recent `n` CVD points, oldest→newest.

        Each entry is `(open_time, cvd_or_None)`; `open_time` is the
        anchor candle of that window. To get `n` points we need
        `n + WINDOW - 1` raw candles (so even the oldest anchor has a
        full trailing window). The check for "is this window valid" is
        applied to each anchor independently — a single hole inside
        the dataset only blanks out the windows that actually contain
        it, the others stay clean.

        Vectorized with pandas rolling: the older per-anchor Python loop
        was O(n × WINDOW), which crossed the request budget once n grew
        to a year of 5m anchors (~105k × 200 ≈ 20M Decimal adds). The
        rolling-sum path is O(n) and runs comfortably inside an HTTP
        round-trip even at the full 1-year resolution.
        """
        symbol, interval = self._validate(symbol, interval)
        if n < 1:
            raise ValueError("n must be >= 1")

        total = n + self.WINDOW - 1
        rows = self._fetch_window(symbol, interval, total=total)

        # If we couldn't even gather enough candles to fill the oldest
        # window, every anchor is incomplete by definition. Return a
        # series of Nones at the timestamps we *do* have, so the caller
        # still gets an aligned x-axis to plot against.
        if len(rows) < total:
            tail = rows[-n:] if rows else []
            return [(r["open_time"], None) for r in tail]

        df = pd.DataFrame(rows)

        # Decimal → float64. `pd.to_numeric` turns None into NaN, which
        # then propagates into the rolling sum as NaN so any anchor whose
        # 200-bar window touches a missing delta blanks out automatically.
        # We accept the float round-trip here because `cvd_payload` already
        # casts the value through `float()` at serialization time, so the
        # JSON payload is no less precise than it ever was.
        delta_f = pd.to_numeric(df["delta"])

        # Rolling sum over WINDOW bars; min_periods=WINDOW ensures the
        # first WINDOW-1 anchors (which can't have a full trailing window
        # within this slice) come out as NaN.
        cvd_f = delta_f.rolling(window=self.WINDOW, min_periods=self.WINDOW).sum()

        # Gap detection: anchor i has a clean window iff
        # `open_time[i] - open_time[i - (WINDOW-1)]` equals the expected
        # span. The `uniq_candle` constraint on (symbol, interval,
        # open_time) rules out duplicates, so equal span over WINDOW rows
        # implies a contiguous grid — one shift+compare instead of 199
        # pairwise diffs per anchor.
        expected_span = pd.Timedelta(minutes=(self.WINDOW - 1) * self._INTERVAL_MINUTES[interval])
        span = df["open_time"] - df["open_time"].shift(self.WINDOW - 1)
        cvd_f = cvd_f.where(span == expected_span, other=float("nan"))

        # Emit the trailing n anchors. Convert each finite float back to
        # Decimal so the public type stays `Decimal | None`; NaN → None.
        anchor_times = df["open_time"].iloc[-n:].tolist()
        anchor_cvds = cvd_f.iloc[-n:].tolist()
        return [
            (t, None if (v != v) else Decimal(str(v)))  # v != v: NaN check
            for t, v in zip(anchor_times, anchor_cvds, strict=True)
        ]

    def series_for_lookback(
        self, symbol: str, interval: str, days: int
    ) -> list[tuple[datetime, Decimal | None]]:
        """Convenience wrapper: return CVD anchors covering the last `days`.

        Computes the anchor count from the interval's native cadence and
        delegates to `series`. Lets callers (e.g. the chart view) ask for
        "one year of CVD" without leaking the interval→minutes table
        across module boundaries.
        """
        symbol, interval = self._validate(symbol, interval)
        if days < 1:
            raise ValueError("days must be >= 1")
        minutes = self._INTERVAL_MINUTES[interval]
        # Round up so a fractional final bar still gets an anchor — the
        # series's own "not enough rows" path handles the under-filled
        # case if the DB hasn't caught up to `days` yet.
        n = -(-(days * 24 * 60) // minutes)
        return self.series(symbol=symbol, interval=interval, n=n)

    # ---- internals --------------------------------------------------------
    def _validate(self, symbol: str, interval: str) -> tuple[str, str]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if interval not in self.ALLOWED_INTERVALS:
            # `1M` is explicitly excluded here even though it's a valid
            # Candle.interval — see _INTERVAL_MINUTES rationale.
            raise ValueError(f"interval must be one of {sorted(self.ALLOWED_INTERVALS)}")
        return normalized_symbol, interval

    @staticmethod
    def _fetch_window(symbol: str, interval: str, total: int) -> list[dict]:
        """Return up to `total` newest (open_time, delta) rows, oldest→newest.

        We fetch newest-first via the existing `candle_lookup_idx`
        (`data/models/candle.py:67-72`) then reverse in Python, so the
        slice respects the DB index and the caller still sees an
        oldest→newest list ready for windowing.
        """
        rows = list(
            Candle.objects.filter(symbol=symbol, interval=interval)
            .order_by("-open_time")
            .values("open_time", "delta")[:total]
        )
        rows.reverse()
        return rows

    def _sum_or_none(self, window: list[dict], interval: str) -> Decimal | None:
        """Sum `delta` across `window`, or return `None` if incomplete.

        Gap detection is `len == WINDOW` AND `(last − first) == (WINDOW−1)
        × interval`. The `uniq_candle` constraint on (symbol, interval,
        open_time) rules out duplicates, so equal length plus equal span
        implies a fully contiguous grid — one subtraction instead of 199
        pairwise diffs.
        """
        if len(window) != self.WINDOW:
            return None
        expected_span = timedelta(minutes=(self.WINDOW - 1) * self._INTERVAL_MINUTES[interval])
        if window[-1]["open_time"] - window[0]["open_time"] != expected_span:
            return None
        if any(r["delta"] is None for r in window):
            return None
        return sum((r["delta"] for r in window), Decimal(0))
