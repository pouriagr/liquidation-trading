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

        expected_span = timedelta(minutes=(self.WINDOW - 1) * self._INTERVAL_MINUTES[interval])

        out: list[tuple[datetime, Decimal | None]] = []
        for i in range(n):
            window = rows[i : i + self.WINDOW]
            anchor_time = window[-1]["open_time"]
            cvd: Decimal | None
            if window[-1]["open_time"] - window[0]["open_time"] == expected_span and all(
                r["delta"] is not None for r in window
            ):
                cvd = sum((r["delta"] for r in window), Decimal(0))
            else:
                cvd = None
            out.append((anchor_time, cvd))
        return out

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
