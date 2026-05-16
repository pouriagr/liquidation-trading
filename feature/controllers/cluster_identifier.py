"""Liquidation cluster identification — math + persistence layer.

Implements `docs/liquidation_framework_concept.md` §5 end-to-end: take
the foundational signals (OI history, candles, funding) and produce a
list of time-bounded clusters where leveraged positions are likely to
be force-closed. The math lives in `feature.services.clustering`; this
controller is the DB I/O layer for both reads (raw OI / candles /
funding) and writes (persisted `ClusterSegment` rows).

Two public entry points:

  * `identify(symbol, lookback_hours, *, now=None)` — compute and
    return an in-memory `ClusterMap` for the rolling window
    `[now - lookback_hours, now]`. Pure read; persists nothing. Used
    by tests, by the backtest harness (passes a fixed `now`), and by
    anything that wants the freshest possible view without touching
    the DB write path.

  * `compute_and_persist(symbol, lookback_hours, *, now=None)` — same
    compute, then atomically replace the persisted `ClusterSegment`
    rows for that `(symbol, lookback_hours)` scope. Wired into
    `RefreshController`, which loops `SUPPORTED_LOOKBACKS` so a
    Refresh click rebuilds all three windows along with the candles /
    OI / funding they depend on.

The previous architecture computed clusters live on every GET, which
didn't scale to full chart history (~year of 1 h OI + ~year of 5 m
candles = ~500 ms per request and ~1.5 MB JSON). With persistence,
GETs reduce to a single indexed SELECT on
`data.ClusterSegment(symbol)`.

The scan range is the **full** 1 h OI history in the DB —
`identify(symbol, lookback_hours)` walks every row, just as the
pre-multi-window code did, so historical anchors are still candidates
for cluster zones. `compute_and_persist` runs once per member of
`SUPPORTED_LOOKBACKS = (24, 72, 168)` on every refresh, tagging each
segment with the window that produced it.

What the lookback knob actually controls — re-reading the doc — is
the §5.2 **per-anchor percentile-calibration window**: each ΔOI is
judged against the 90th percentile of deltas in its own trailing
`lookback_hours`. A quiet hour from six months ago competes against
the contemporaneous baseline around it, not against today's absolute
OI scale (§5.2: "Look at OI changes over a recent lookback period —
typical ranges: 24 hours to 7 days"). A 24 h-window run qualifies
different historical hours than a 168 h-window run; the chart shows
the full year of `ClusterSegment` rectangles either way, with their
`start_time`/`end_time` from §5 driving the time-axis rendering.

Multiple windows are surfaced through the GET endpoint, which sums
strengths per `(price_band, side)` across the selected subset —
that's the §5.4 "sum of contributions" rule applied along the window
axis, and it realises the §12.3 multi-resolution confluence boost
without an ad-hoc multiplier: a band that qualified under all three
calibration windows accumulates ~3× the single-window strength.

`identify` also accepts an optional `now: datetime | None = None` so
a future backtest harness can compute clusters as-of any historical
anchor (drives `generated_at`, the density-gate span, and the
latest-close anchor); the web/refresh path leaves it `None` and the
controller defaults to `_now()`.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.db import transaction

from data.models import Candle, FundingRate, OpenInterest, Symbol
from data.models import ClusterSegment as ClusterSegmentRow
from feature.services.clustering import (
    LEVERAGE_PMF,
    AccumulationZone,
    ClusterSegment,
    aggregate_5m_to_1h_candles,
    assemble_segments,
    compute_positive_deltas,
    direction_bias,
    rolling_threshold,
)

logger = logging.getLogger(__name__)


@dataclass
class ClusterMap:
    """In-memory §5 output for one symbol at one point in time.

    Used by `identify()` and the math-layer tests. The persistence
    path (`compute_and_persist`) writes the `segments` list into the
    `data.ClusterSegment` table; `anchor_price` and `generated_at` are
    not persisted (anchor is recomputable from the latest close;
    generated_at is captured via the ORM's `auto_now_add` per row).
    """

    symbol: str
    generated_at: datetime
    anchor_price: Decimal
    zones: list[AccumulationZone]
    segments: list[ClusterSegment]


class ClusterIdentifierController:
    """Computes §5 cluster maps; reads raw inputs, writes persisted segments."""

    # The 1h OI period — already derived by OIAggregatorController on every
    # refresh, see `feature/controllers/refresh.py:_derive_oi_1h`. We don't
    # aggregate ourselves; we read what was prepared.
    OI_PERIOD = "1h"
    # 5m is the only candle interval this controller fetches. The §5.3
    # direction inference and the §5.2 zone price range both want a
    # 1h-resolution OHLC, but we *derive* that from 5m candles via
    # `aggregate_5m_to_1h_candles` — same pattern as 1h OI being
    # derived from 5m OI by `OIAggregatorController`. Keeps the refresh
    # bundle doc-aligned (`CANDLE_INTERVALS = ("5m", "15m", "4h", "1d")`,
    # per §12.3) without needing to fetch a separate 1h candle source.
    # 5m is indexed by `candle_lookup_idx` (`data/models/candle.py:80`),
    # so the range scan stays cheap even at a year of history (~105k
    # rows). It's also the right resolution for sweep detection — an
    # intra-hour wick still consumes a level, which is what a
    # chart-watcher expects.
    CONSUMPTION_INTERVAL = "5m"

    # The §12.2 "1 h analysis with 24h–168h lookback" choices the chart
    # UI exposes. Each refresh runs the §5 pipeline once per member,
    # tagging persisted segments with their `lookback_hours`. The GET
    # endpoint accepts any non-empty subset and aggregates server-side
    # (sum of strengths per band, mirroring §5.4 and realising the
    # §12.3 confluence boost without an ad-hoc multiplier).
    SUPPORTED_LOOKBACKS: tuple[int, ...] = (24, 72, 168)

    # Legacy default for the §5.2 sliding-percentile window, kept so
    # any caller that constructs zones outside the per-window
    # `identify` path (none today, but the `_build_zones` signature
    # is reusable) sees a sensible fallback. The new path always
    # passes `window_hours=lookback_hours` so this constant is unused
    # by the production flow.
    THRESHOLD_WINDOW_DAYS = 7

    # §5.2 threshold percentile and the §5.5 / §5.4 tuning constants.
    # Kept as class attributes (not module-level) so a test or admin
    # override can patch a single instance without leaking state to
    # the singleton.
    PERCENTILE_THRESHOLD = 90
    # 0.50% source bins. The prior 0.10% was too granular for the chart —
    # produced ~44k segments per symbol, an unreadable wall of overlapping
    # bands. Wider bins both reduce segment count ~5× *and* produce
    # visually meaningful zone widths (~$400 at BTC $80k vs ~$80). The
    # heatmap aggregator on the chart side (`home.js`) uses the same
    # 0.5% pitch for its `(time, price)` cell grid, so each source band
    # maps to exactly one heatmap row.
    PRICE_BAND_PCT = Decimal("0.005")
    MAX_LEVERAGE_TIER = 100
    # Recency half-life is retained as a public constant in case a future
    # "decay old levels" UI toggle wants to re-apply the §5.5 recency
    # discount client-side. The default `assemble_segments` pipeline no
    # longer multiplies strength by `recency_weight` — historical bands
    # need to compete with recent ones on intrinsic magnitude so the
    # chart remains useful for backtest viewing. See the long-form note
    # in `feature.services.clustering.assemble_segments`'s docstring.
    RECENCY_HALFLIFE_HOURS = 72.0

    # Density gate for the 5m consumption tape. 288 bars/day is the
    # Binance 5m cadence; we expect roughly `span_days · 288` rows
    # across the active scan. When the actual count falls below
    # `CONSUMPTION_DENSITY_FLOOR` of that expectation,
    # `find_consumption_time` is structurally vulnerable to missing
    # sweeps inside the gaps. The floor is 0.90 to mirror the refresh
    # pipeline's own density gate on the 5m source.
    CONSUMPTION_ROWS_PER_DAY = 288
    CONSUMPTION_DENSITY_FLOOR = 0.9

    # Sign-floor / strong-move thresholds for `direction_bias`. 5 bps =
    # "essentially flat, go to funding tie-break"; 50 bps = "this counts
    # as a strong directional accumulation hour, saturate the bias".
    SIGN_FLOOR_BPS = 5
    STRONG_MOVE_BPS = 50

    # Bulk-create batch size for the persist path — keeps memory bounded
    # on a year-long recompute where a single symbol can yield several
    # thousand segments. Postgres handles batches this size in one
    # round-trip without notable overhead.
    PERSIST_BATCH_SIZE = 500

    ALLOWED_SYMBOLS = frozenset(Symbol.values)

    # ---- public entry points ------------------------------------------------
    def identify(
        self,
        symbol: str,
        lookback_hours: int,
        *,
        now: datetime | None = None,
    ) -> ClusterMap:
        """Compute the §5 cluster map for `symbol` over the full OI history.

        Read-only — persists nothing. `compute_and_persist` wraps this
        and writes the result to `data.ClusterSegment`. The scan range
        is the full 1h OI history in the DB; `lookback_hours` controls
        only the §5.2 90th-percentile **threshold calibration window**
        that slides per anchor. So a 24h-window run qualifies every
        historical hour whose ΔOI beat its own trailing-24h percentile,
        a 168h-window qualifies hours whose ΔOI beat the trailing-168h
        percentile, and so on. Different windows produce different
        qualifying-hour sets — that's where the §12.3 multi-resolution
        confluence comes from when the GET endpoint sums their
        strengths per band.

        `lookback_hours` must be a member of `SUPPORTED_LOOKBACKS`.
        `now` defaults to the wall-clock current time; passing a fixed
        timestamp lets a backtest harness compute clusters as-of any
        historical anchor (used for `generated_at`, the density-gate
        span, and the latest-close anchor).

        Raises `ValueError` on unknown symbol or unsupported lookback.
        Returns an empty `ClusterMap` when the DB has too little data
        to derive anything — fewer than two 1h OI rows, no latest
        candle close, no qualifying accumulation hours, etc.
        """
        symbol = self._validate_symbol(symbol)
        if lookback_hours not in self.SUPPORTED_LOOKBACKS:
            raise ValueError(
                f"lookback_hours must be one of {list(self.SUPPORTED_LOOKBACKS)} "
                f"(got {lookback_hours})"
            )
        now = now or self._now()

        oi_rows = self._fetch_oi_1h(symbol)
        if len(oi_rows) < 2:
            logger.info(
                "cluster_identify: %s lb=%dh — not enough 1h OI rows (%d) in DB",
                symbol,
                lookback_hours,
                len(oi_rows),
            )
            return self._empty_map(symbol, anchor=self._latest_close(symbol) or Decimal(0), now=now)

        anchor = self._latest_close(symbol)
        if anchor is None or anchor <= 0:
            logger.info(
                "cluster_identify: %s lb=%dh — no latest candle close for anchor",
                symbol,
                lookback_hours,
            )
            return self._empty_map(symbol, anchor=Decimal(0), now=now)

        # Fetch the 5m OHLC tape ONCE over the full OI span. It feeds
        # two consumers:
        #   1. Zone-building, via `aggregate_5m_to_1h_candles` for
        #      per-anchor 1h OHLC (§5.2 price range + §5.3 direction).
        #   2. Sweep detection, via a (time, low, high) projection of
        #      the same list, passed to `find_consumption_time`.
        # Starting at the earliest 1h OI timestamp means every
        # historical anchor that qualifies under its own percentile
        # baseline has the candle data it needs — both for the zone's
        # 1h OHLC and for the sweep-clip search that may extend years
        # into the future (the segment stays alive until a 5m wick
        # crosses it).
        candles_5m_ohlc = self._fetch_5m_ohlc(symbol, start=oi_rows[0][0])

        zones = self._build_zones(
            symbol,
            oi_rows,
            candles_5m_ohlc,
            lookback_hours=lookback_hours,
        )
        if not zones:
            logger.info(
                "cluster_identify: %s lb=%dh — no significant accumulation hours",
                symbol,
                lookback_hours,
            )
            return ClusterMap(
                symbol=symbol,
                generated_at=now,
                anchor_price=anchor,
                zones=[],
                segments=[],
            )

        # Density check before we lean on the tape for sweep
        # detection. A thin tape doesn't make the run *wrong*, but
        # every open-ended segment becomes structurally suspect.
        # Gate against the full OI horizon, since the 5m tape covers
        # the same span (`oi_rows[0][0]` → `now`).
        consumption_start = oi_rows[0][0]
        span_seconds = max(1.0, (now - consumption_start).total_seconds())
        span_days = span_seconds / 86400.0
        expected_5m = max(1, int(span_days * self.CONSUMPTION_ROWS_PER_DAY))
        if len(candles_5m_ohlc) < expected_5m * self.CONSUMPTION_DENSITY_FLOOR:
            logger.warning(
                "cluster_identify: %s lb=%dh sparse 5m tape %d/%d (%.0f%%) span=%.1fd — "
                "open-ended segments may hide real sweeps inside gaps",
                symbol,
                lookback_hours,
                len(candles_5m_ohlc),
                expected_5m,
                len(candles_5m_ohlc) / expected_5m * 100,
                span_days,
            )

        # Project to the (time, low, high) shape `find_consumption_time`
        # expects. Cheap O(N); avoids a second DB query for the same
        # candles in a different column subset.
        consumption_candles = [(t, lo, hi) for t, _o, hi, lo, _c in candles_5m_ohlc]

        # Sweep-clipping (the only time-bound mechanism on strength)
        # is layered inside `assemble_segments` via
        # `find_consumption_time`. §5.6's recency decay was previously
        # also applied here but produced a chart that washed out
        # historical bands — see the long-form note in
        # `feature.services.clustering.assemble_segments`'s docstring.
        # `now` is still passed through for the segment lifecycle
        # math; the strength itself no longer depends on it.
        segments = assemble_segments(
            zones,
            now=now,
            anchor=anchor,
            band_pct=self.PRICE_BAND_PCT,
            consumption_candles=consumption_candles,
            leverage_pmf=LEVERAGE_PMF,
            max_tier=self.MAX_LEVERAGE_TIER,
        )

        logger.info(
            "cluster_identify: %s lb=%dh zones=%d segments=%d candles_5m=%d "
            "span=%.1fd anchor=%s",
            symbol,
            lookback_hours,
            len(zones),
            len(segments),
            len(candles_5m_ohlc),
            span_days,
            anchor,
        )
        return ClusterMap(
            symbol=symbol,
            generated_at=now,
            anchor_price=anchor,
            zones=zones,
            segments=segments,
        )

    @transaction.atomic
    def compute_and_persist(
        self,
        symbol: str,
        lookback_hours: int,
        *,
        now: datetime | None = None,
    ) -> dict:
        """Re-derive the cluster map for `(symbol, lookback_hours)` and
        replace persisted rows for that scope.

        Wrapped in a single transaction so a mid-write failure rolls
        back to the prior state — the chart keeps showing the
        previous (still-valid, just slightly stale) set rather than
        an empty list. The DELETE narrows to
        `(symbol=?, lookback_hours=?)` so the other supported windows
        for the same symbol are untouched; the refresh hook calls this
        once per member of `SUPPORTED_LOOKBACKS`. Returns a
        `{deleted, created, anchor_price, lookback_hours}` summary
        for `RefreshController` to fold into its source report.

        Replacement strategy is delete-then-bulk_create rather than
        per-row update: the math may re-evaluate older zones against
        fresh sweep candles (a sweep observed this refresh that
        wasn't there last refresh shortens an existing segment), so
        a row-level diff isn't trivially correct. A full replace
        keeps the persisted set consistent with what `identify()`
        would produce live.
        """
        cmap = self.identify(symbol, lookback_hours, now=now)

        qs = ClusterSegmentRow.objects.filter(
            symbol=cmap.symbol,
            lookback_hours=lookback_hours,
        )
        deleted_count = qs.count()
        qs.delete()

        rows = [
            ClusterSegmentRow(
                symbol=cmap.symbol,
                side=s.side,
                price_low=s.price_low,
                price_high=s.price_high,
                price=s.price,
                start_time=s.start_time,
                end_time=s.end_time,
                source_open_time=s.source_open_time,
                strength=s.strength,
                notional=s.notional,
                long_bias=s.long_bias,
                lookback_hours=lookback_hours,
            )
            for s in cmap.segments
        ]
        if rows:
            ClusterSegmentRow.objects.bulk_create(rows, batch_size=self.PERSIST_BATCH_SIZE)

        logger.info(
            "cluster_persist: %s lb=%dh deleted=%d created=%d anchor=%s",
            cmap.symbol,
            lookback_hours,
            deleted_count,
            len(rows),
            cmap.anchor_price,
        )
        return {
            "deleted": deleted_count,
            "created": len(rows),
            "anchor_price": float(cmap.anchor_price),
            "lookback_hours": lookback_hours,
        }

    # ---- internals — zone building -----------------------------------------
    def _build_zones(
        self,
        symbol: str,
        oi_rows: list[tuple[datetime, Decimal]],
        candles_5m_ohlc: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
        *,
        lookback_hours: int,
    ) -> list[AccumulationZone]:
        """Run §5.2 + §5.3 over the prepared rows; return one zone per
        significant accumulation hour.

        The OI rows are oldest→newest and already bounded to the
        `[now - lookback_hours, now]` window by the caller.
        `compute_positive_deltas` produces the pairwise positive
        deltas. `rolling_threshold` computes a per-anchor §5.2 cutoff
        using `window_hours=lookback_hours`, so the qualification bar
        is the 90th percentile of deltas inside the same window the
        scan covers — §5.2 ("Look at OI changes over a recent
        lookback period") applies literally.

        The 1h OHLC needed for §5.3 (open / close → direction bias)
        and §5.2 (high / low → zone price range) is *derived* from
        the 5m candle tape via `aggregate_5m_to_1h_candles` — same
        pattern as 1h OI being derived from 5m OI. Keeps the refresh
        bundle (`CANDLE_INTERVALS = ("5m", "15m", "4h", "1d")`)
        doc-aligned without needing a separate 1h candle source.
        """
        positive = compute_positive_deltas(oi_rows)
        if not positive:
            return []

        thresholds_by_anchor = rolling_threshold(
            positive,
            window_hours=float(lookback_hours),
            percentile=self.PERCENTILE_THRESHOLD,
        )

        anchors = [
            bar_open
            for bar_open, delta in positive
            if delta >= thresholds_by_anchor.get(bar_open, Decimal(0))
        ]
        if not anchors:
            return []

        # Derive 1h OHLC for the qualifying anchors from the 5m tape
        # we already loaded in `identify()`. Same shape as the prior
        # `_fetch_candles_for_anchors` returned; everything downstream
        # is unchanged.
        candle_by_anchor = aggregate_5m_to_1h_candles(candles_5m_ohlc, anchors)
        # Funding tie-break: load only the funding rates we might need —
        # one per anchor — in a single ranged query.
        funding_by_anchor = self._fetch_funding_for_anchors(symbol, anchors)

        zones: list[AccumulationZone] = []
        for bar_open, delta in positive:
            if delta < thresholds_by_anchor.get(bar_open, Decimal(0)):
                continue
            candle = candle_by_anchor.get(bar_open)
            if candle is None:
                # No 5m candles in this whole hour — couldn't derive
                # an OHLC aggregate. Treat as "ambiguous": skip
                # rather than fabricate a zone with no price range.
                # Should be very rare once the 5m refresh density
                # gate (90%) is satisfied; if it fires often the
                # `CONSUMPTION_DENSITY_FLOOR` warning in `identify()`
                # already surfaces the underlying data-quality issue.
                logger.debug(
                    "cluster_identify: %s no 5m candles for %s — skipping zone",
                    symbol,
                    bar_open.isoformat(),
                )
                continue
            bias = direction_bias(
                price_open=candle["open"],
                price_close=candle["close"],
                sign_floor_bps=self.SIGN_FLOOR_BPS,
                strong_move_bps=self.STRONG_MOVE_BPS,
                funding_rate=funding_by_anchor.get(bar_open),
            )
            zones.append(
                AccumulationZone(
                    open_time=bar_open,
                    price_low=candle["low"],
                    price_high=candle["high"],
                    delta_oi_notional=delta,
                    long_bias=bias,
                )
            )
        return zones

    # ---- internals — DB reads ----------------------------------------------
    def _fetch_oi_1h(self, symbol: str) -> list[tuple[datetime, Decimal]]:
        """Return every 1h OI row for `symbol`, oldest→newest.

        No `timestamp__gte` filter: the §5 pipeline scans the entire
        OI history so historical anchors can qualify under their own
        contemporaneous percentile baseline (§5.2's "OI changes over
        a recent lookback period" is a per-anchor calibration window,
        not a clamp on the scan range). The horizon is whatever the
        refresh pipeline has backfilled (~12-month metrics archive
        plus the live tail). Single indexed range scan via
        `oi_lookup_idx`.
        """
        rows = list(
            OpenInterest.objects.filter(
                symbol=symbol,
                period=self.OI_PERIOD,
            )
            .order_by("-timestamp")
            .values("timestamp", "sum_open_interest_value")
        )
        rows.reverse()
        return [(r["timestamp"], r["sum_open_interest_value"]) for r in rows]

    def _fetch_5m_ohlc(
        self,
        symbol: str,
        *,
        start: datetime,
    ) -> list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]]:
        """All 5m OHLC rows for `symbol` from `start` onward, oldest→newest.

        Returns `(open_time, open, high, low, close)`. Single indexed
        range scan via `candle_lookup_idx` on
        `(symbol, interval="5m", -open_time)`; we reverse in Python
        the same way `_fetch_oi_1h` does.

        One shared 5m fetch serves both consumers in `identify()`:

        - `_build_zones` calls `aggregate_5m_to_1h_candles` to derive
          the 1h OHLC needed for §5.3 direction inference and §5.2
          price range (the math layer mirrors `OIAggregatorController`
          deriving 1h OI from 5m OI).
        - Sweep detection projects this list down to `(time, low,
          high)` tuples and feeds them to `find_consumption_time`.

        `start` is the earliest 1h OI timestamp; older candles can't
        contribute to either consumer (zones can't exist before OI,
        and zones can't be swept before they exist). The 5m tape
        spans the same horizon as the OI scan so historical zones
        get the full window of subsequent 5m candles in which a
        sweep could occur — the lookback knob does NOT clamp this
        range, only the §5.2 percentile-calibration window.
        """
        rows = list(
            Candle.objects.filter(
                symbol=symbol,
                interval=self.CONSUMPTION_INTERVAL,
                open_time__gte=start,
            )
            .order_by("-open_time")
            .values("open_time", "open", "high", "low", "close")
        )
        rows.reverse()
        return [(r["open_time"], r["open"], r["high"], r["low"], r["close"]) for r in rows]

    def _fetch_funding_for_anchors(
        self, symbol: str, anchors: list[datetime]
    ) -> dict[datetime, Decimal]:
        """Latest funding rate at or before each anchor.

        Binance settles funding every 8h on USDT-M perpetuals, so
        each accumulation hour falls inside some 8h funding window —
        the sign of the most-recent settlement at the time gives
        the regime the §5.3 tie-break needs. To avoid an N+1 query,
        we load all funding rows in the span and resolve via a
        binary search in Python.
        """
        if not anchors:
            return {}
        span_start = min(anchors) - timedelta(days=1)  # one extra day so the
        # very first anchor's "most recent prior settlement" is reachable.
        span_end = max(anchors) + timedelta(hours=1)
        rows = list(
            FundingRate.objects.filter(
                symbol=symbol,
                funding_time__gte=span_start,
                funding_time__lte=span_end,
            )
            .order_by("funding_time")
            .values("funding_time", "funding_rate")
        )
        if not rows:
            return {}
        # Binary-search each anchor against the sorted funding timestamps.
        times = [r["funding_time"] for r in rows]
        rates = [r["funding_rate"] for r in rows]
        out: dict[datetime, Decimal] = {}
        for a in anchors:
            # The funding row whose `funding_time <= a` is the regime in
            # effect — bisect_right gives the insertion index for `a` in
            # the sorted list, so the last row with time <= a is at idx-1.
            idx = bisect.bisect_right(times, a) - 1
            if idx >= 0:
                out[a] = rates[idx]
        return out

    def _latest_close(self, symbol: str) -> Decimal | None:
        """Most recent 5m close for `symbol`.

        Used as the geometric anchor for the band/bucket grids — a
        global anchor keeps the grid stable when two zones at
        slightly different prices contribute to the same band.
        5m is the cluster code's canonical candle source (see
        `CONSUMPTION_INTERVAL`) and is maintained by every refresh,
        so this anchor stays fresh; the previous 1h source was the
        only thing in this controller that asked for 1h candles and
        is no longer needed. Returns `None` if no 5m candle exists;
        the public method translates that into an empty cluster map.
        """
        row = (
            Candle.objects.filter(symbol=symbol, interval=self.CONSUMPTION_INTERVAL)
            .order_by("-open_time")
            .values("close")
            .first()
        )
        return row["close"] if row else None

    # ---- internals — validation / utilities --------------------------------
    def _validate_symbol(self, symbol: str) -> str:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized = symbol.strip().upper()
        if normalized not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        return normalized

    def _empty_map(
        self,
        symbol: str,
        *,
        anchor: Decimal,
        now: datetime | None = None,
    ) -> ClusterMap:
        return ClusterMap(
            symbol=symbol,
            generated_at=now or self._now(),
            anchor_price=anchor,
            zones=[],
            segments=[],
        )

    @staticmethod
    def _now() -> datetime:
        """Single source of "now" so a test can patch one method."""
        return datetime.now(UTC)
