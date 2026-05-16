"""Multi-source refresh orchestrator for the 15m trade-decision pipeline.

Per `docs/liquidation_framework_concept.md` §12.3, a trade decision at the
fifteen-minute candle close consumes a *fixed bundle* of data inputs at
each input's own natural resolution. This controller is the single entry
point that brings the whole bundle up to date in one call.

The bundle this orchestrator owns:

  * Candles at 5m, 15m, 4h, 1d  (5m + 15m for CVD divergence;
    4h + 1d for higher-timeframe context)
  * Open Interest at 5m         (the only period Binance publishes
    historically; 1h is *derived* from 5m and persisted alongside)
  * Funding rate (full history) (single stream; Binance dictates cadence)

CVD is *not* a separate source here — `Candle.delta` is auto-populated by
`feature.signals.set_candle_delta` on every candle save, and CVD itself
is read on demand via `feature.controllers.cvd_controller`.

For each source the strategy is the same:

  1. Count rows for the source in the `[now − LOOKBACK_DAYS, now]`
     window and compare against the expected count derived from that
     source's native cadence (see `_covers_lookback`).
  2. If recent coverage is dense (≥ `DENSITY_THRESHOLD` of expected) —
     call only the live `fetch_*` controller to top up the tail. Cheap,
     fast, idempotent.
  3. Otherwise — call the matching `backfill_*` controller first, *then*
     the live fetch. The archive controllers cover history up to a
     recent boundary; the live fetch closes the small gap between that
     boundary and "now".

Counting rows (rather than just inspecting min/max timestamps) catches
the orphan-island case: a stray ancient row + a thin recent tail with
a year-long empty gap in between would otherwise pass an earliest-only
check and silently skip the backfill that's actually needed.

After the 5m OI step succeeds, the 1h OI rows are re-derived via
`OIAggregatorController` over the recent window and persisted as
`OpenInterest(period='1h')` rows in the same table.

The orchestrator lives in `feature/` rather than `data/` because it
*both* triggers raw ingestion (via `data.controllers.*` — `feature → data`
imports are allowed) *and* persists a derived signal (1h OI). Hosting it
in `data/` would require a backwards `data → feature` import for the
aggregator step, breaking the convention. See AGENTS.md.

Per-source exceptions are caught and recorded into `SourceResult.error`
rather than raised — one upstream failure does not kill the rest of the
refresh, matching the "max possible data" intent.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from data.controllers import (
    binance_candles_controller,
    binance_funding_rate_controller,
    binance_klines_archive_controller,
    binance_metrics_archive_controller,
    binance_open_interest_controller,
)
from data.models import Candle, FundingRate, OpenInterest, Symbol
from feature.controllers.cluster_identifier import ClusterIdentifierController
from feature.controllers.oi_aggregator import OIAggregatorController

logger = logging.getLogger(__name__)


@dataclass
class SourceResult:
    """Per-source row in the refresh summary."""

    label: str  # human-friendly: "candles 15m", "oi 5m", "funding", "oi 1h (derived)"
    backfilled: bool  # did we call the backfill controller this run?
    received: int  # rows pulled from upstream this run (0 for the derived row)
    created: int  # newly inserted rows
    updated: int  # in-place upserts
    error: str | None = None  # populated when this source raised; others still run


@dataclass
class RefreshResult:
    symbol: str
    decision_interval: str
    sources: list[SourceResult] = field(default_factory=list)


class RefreshController:
    """Orchestrates the 15m bundle: candles + OI + funding + derived 1h OI."""

    # How far back the DB should already cover before we skip the backfill
    # and only top up the tail. One year matches "enough for a 1y backtest"
    # — the user-stated requirement that drove this controller.
    LOOKBACK_DAYS = 365

    # Minimum row-count fraction (relative to the cadence-derived expected
    # count) the lookback window must already hold before we skip the
    # backfill. Tolerates ~10% holes — Binance archive 404s for individual
    # days, pairs that listed mid-window, etc. — while still catching the
    # orphan-island case where a stray ancient row tricks an earliest-only
    # check (recent density there comes out near 0%).
    DENSITY_THRESHOLD = 0.90

    # Rows-per-day for each source, used to compute the expected count
    # over `LOOKBACK_DAYS`. Keeping the cadence table here (rather than
    # importing it from `data/`) keeps the orchestrator independent of
    # any one ingest controller's private constants — `data.controllers`
    # owns the fetch rhythm; we own the coverage policy.
    _CANDLE_ROWS_PER_DAY: dict[str, float] = {
        "5m": 288, "15m": 96, "30m": 48, "1h": 24,
        "2h": 12, "4h": 6, "6h": 4, "8h": 3,
        "12h": 2, "1d": 1,
    }  # fmt: skip
    _OI_ROWS_PER_DAY: dict[str, float] = {"5m": 288, "1h": 24}
    # Binance USDT-M perpetuals settle funding every 8h → 3 rows/day.
    _FUNDING_ROWS_PER_DAY: float = 3.0

    # The framework's decision rhythm. Refresh is hard-gated to this
    # interval; the controller raises ValueError on any other value so the
    # chart's per-interval refresh button can be disabled safely.
    DECISION_INTERVAL = "15m"

    # The fixed bundle — source of truth for "what 15m trading needs".
    CANDLE_INTERVALS: tuple[str, ...] = ("5m", "15m", "4h", "1d")
    OI_PERIODS: tuple[str, ...] = ("5m",)  # 1h is derived, not fetched.

    # Per-source fetch caps. Mirrors each controller's MAX_LIMIT — kept
    # here so the orchestration is self-documenting and changing one
    # doesn't silently change the other.
    CANDLE_FETCH_LIMIT = 1500
    OI_FETCH_LIMIT = 500
    FUNDING_FETCH_LIMIT = 1000

    ALLOWED_SYMBOLS = frozenset(Symbol.values)

    def __init__(
        self,
        oi_aggregator: OIAggregatorController | None = None,
        cluster_identifier: ClusterIdentifierController | None = None,
    ) -> None:
        # Both dependencies are DI'd so tests can substitute stubs
        # without patching at the import level. Defaults are real
        # because production callers don't need to know they exist.
        self._oi_aggregator = oi_aggregator or OIAggregatorController()
        self._cluster_identifier = cluster_identifier or ClusterIdentifierController()

    # ---- public entry point -------------------------------------------------
    def refresh(self, symbol: str, interval: str) -> RefreshResult:
        """Bring the full 15m bundle up to date for `symbol`.

        `interval` must equal `DECISION_INTERVAL` ("15m"). It's accepted
        as a parameter (rather than ignored) so callers — the chart view
        — can pass through the user's selected interval verbatim and let
        validation reject non-15m calls with a clean error.
        """
        symbol, interval = self._validate(symbol, interval)

        logger.info("refresh start: symbol=%s interval=%s", symbol, interval)
        result = RefreshResult(symbol=symbol, decision_interval=interval)

        for tf in self.CANDLE_INTERVALS:
            result.sources.append(self._refresh_candles(symbol, tf))

        for period in self.OI_PERIODS:
            result.sources.append(self._refresh_oi(symbol, period))

        result.sources.append(self._refresh_funding(symbol))

        # Derive 1h OI from the 5m rows we just (re)freshed. Done so
        # any per-source failure on the 5m OI step shows up explicitly
        # — and the derived row makes it clear when the input wasn't
        # actually refreshed (zero received → zero created/updated).
        result.sources.append(self._derive_oi_1h(symbol))

        # Recompute and persist §5 liquidation clusters last — depends
        # on the derived 1h OI plus the freshly-ingested 5m candles
        # for sweep detection. Per-source error capture in
        # `_refresh_clusters` keeps a broken cluster step from masking
        # otherwise-successful candles/OI/funding.
        result.sources.append(self._refresh_clusters(symbol))

        ok = sum(1 for s in result.sources if s.error is None)
        failed = len(result.sources) - ok
        logger.info(
            "refresh done: symbol=%s sources=%d ok=%d failed=%d",
            symbol,
            len(result.sources),
            ok,
            failed,
        )
        return result

    # ---- internals — per-source orchestration -------------------------------
    def _refresh_candles(self, symbol: str, interval: str) -> SourceResult:
        label = f"candles {interval}"
        try:
            recent = Candle.objects.filter(
                symbol=symbol,
                interval=interval,
                open_time__gte=self._lookback_start(),
            ).count()
            expected = self.LOOKBACK_DAYS * self._CANDLE_ROWS_PER_DAY[interval]
            density = (recent / expected) if expected else 0
            logger.info(
                "refresh source start: %s density=%.1f%% (recent=%d expected=%d)",
                label,
                density * 100,
                recent,
                int(expected),
            )
            backfilled = False
            if not self._covers_lookback(recent, expected):
                logger.info("refresh source: %s backfill required", label)
                binance_klines_archive_controller.backfill(
                    symbol=symbol, interval=interval, months=12
                )
                backfilled = True
            fetch = binance_candles_controller.fetch_and_store(
                symbol=symbol, interval=interval, limit=self.CANDLE_FETCH_LIMIT
            )
            logger.info(
                "refresh source done: %s received=%d created=%d updated=%d backfilled=%s",
                label,
                fetch.received,
                fetch.created,
                fetch.updated,
                backfilled,
            )
            return SourceResult(
                label=label,
                backfilled=backfilled,
                received=fetch.received,
                created=fetch.created,
                updated=fetch.updated,
            )
        except Exception as exc:  # noqa: BLE001 — per-source isolation by design
            logger.exception("refresh: %s failed", label)
            return SourceResult(
                label=label,
                backfilled=False,
                received=0,
                created=0,
                updated=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _refresh_oi(self, symbol: str, period: str) -> SourceResult:
        label = f"oi {period}"
        try:
            recent = OpenInterest.objects.filter(
                symbol=symbol,
                period=period,
                timestamp__gte=self._lookback_start(),
            ).count()
            expected = self.LOOKBACK_DAYS * self._OI_ROWS_PER_DAY[period]
            density = (recent / expected) if expected else 0
            logger.info(
                "refresh source start: %s density=%.1f%% (recent=%d expected=%d)",
                label,
                density * 100,
                recent,
                int(expected),
            )
            backfilled = False
            if not self._covers_lookback(recent, expected):
                today = self._now().date()
                start = today - timedelta(days=self.LOOKBACK_DAYS)
                end = today - timedelta(days=1)  # archive controller requires end < today
                logger.info(
                    "refresh source: %s backfill required (%s..%s)",
                    label,
                    start.isoformat(),
                    end.isoformat(),
                )
                binance_metrics_archive_controller.backfill(symbol=symbol, start=start, end=end)
                backfilled = True
            fetch = binance_open_interest_controller.fetch_and_store(
                symbol=symbol, period=period, limit=self.OI_FETCH_LIMIT
            )
            logger.info(
                "refresh source done: %s received=%d created=%d updated=%d backfilled=%s",
                label,
                fetch.received,
                fetch.created,
                fetch.updated,
                backfilled,
            )
            return SourceResult(
                label=label,
                backfilled=backfilled,
                received=fetch.received,
                created=fetch.created,
                updated=fetch.updated,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh: %s failed", label)
            return SourceResult(
                label=label,
                backfilled=False,
                received=0,
                created=0,
                updated=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _refresh_funding(self, symbol: str) -> SourceResult:
        label = "funding"
        try:
            recent = FundingRate.objects.filter(
                symbol=symbol,
                funding_time__gte=self._lookback_start(),
            ).count()
            expected = self.LOOKBACK_DAYS * self._FUNDING_ROWS_PER_DAY
            density = (recent / expected) if expected else 0
            logger.info(
                "refresh source start: %s density=%.1f%% (recent=%d expected=%d)",
                label,
                density * 100,
                recent,
                int(expected),
            )
            # FundingRate has no dedicated backfill controller — the live
            # fetch endpoint already serves full history when given a
            # start_time, and the controller pages internally via _paginate.
            # So "backfill" here is the same controller called in range mode.
            if not self._covers_lookback(recent, expected):
                start_time = self._now() - timedelta(days=self.LOOKBACK_DAYS)
                logger.info(
                    "refresh source: %s backfill required (start_time=%s)",
                    label,
                    start_time.isoformat(),
                )
                ranged = binance_funding_rate_controller.fetch_and_store(
                    symbol=symbol,
                    limit=self.FUNDING_FETCH_LIMIT,
                    start_time=start_time,
                )
                # Follow up with a latest-mode fetch in case the range loop's
                # cursor stopped just shy of "now" (e.g. a fresh settlement
                # landed during paging).
                tail = binance_funding_rate_controller.fetch_and_store(
                    symbol=symbol, limit=self.FUNDING_FETCH_LIMIT
                )
                logger.info(
                    "refresh source done: %s received=%d created=%d updated=%d backfilled=True",
                    label,
                    ranged.received + tail.received,
                    ranged.created + tail.created,
                    ranged.updated + tail.updated,
                )
                return SourceResult(
                    label=label,
                    backfilled=True,
                    received=ranged.received + tail.received,
                    created=ranged.created + tail.created,
                    updated=ranged.updated + tail.updated,
                )
            fetch = binance_funding_rate_controller.fetch_and_store(
                symbol=symbol, limit=self.FUNDING_FETCH_LIMIT
            )
            logger.info(
                "refresh source done: %s received=%d created=%d updated=%d backfilled=False",
                label,
                fetch.received,
                fetch.created,
                fetch.updated,
            )
            return SourceResult(
                label=label,
                backfilled=False,
                received=fetch.received,
                created=fetch.created,
                updated=fetch.updated,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh: %s failed", label)
            return SourceResult(
                label=label,
                backfilled=False,
                received=0,
                created=0,
                updated=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _derive_oi_1h(self, symbol: str) -> SourceResult:
        """Roll up the 5m OI rows we just freshed into 1h rows.

        Re-aggregates the entire recent window every run rather than
        tracking what's changed — the math is cheap (≈8.7k rows per
        year), and a full re-aggregate guarantees that any late-arriving
        5m row from the just-completed fetch is reflected in its hour
        bucket without bookkeeping.
        """
        label = "oi 1h (derived)"
        try:
            logger.info("refresh source start: %s (days=%d)", label, self.LOOKBACK_DAYS)
            res = self._oi_aggregator.aggregate_recent(symbol=symbol, days=self.LOOKBACK_DAYS)
            logger.info(
                "refresh source done: %s read_5m=%d written_1h=%d created=%d updated=%d",
                label,
                res.rows_read_5m,
                res.rows_written_1h,
                res.rows_created,
                res.rows_updated,
            )
            return SourceResult(
                label=label,
                backfilled=False,  # derivation, not a fetch
                received=res.rows_read_5m,
                created=res.rows_created,
                updated=res.rows_updated,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh: %s failed", label)
            return SourceResult(
                label=label,
                backfilled=False,
                received=0,
                created=0,
                updated=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _refresh_clusters(self, symbol: str) -> SourceResult:
        """Recompute and persist §5 liquidation clusters for `symbol`,
        once per member of `ClusterIdentifierController.SUPPORTED_LOOKBACKS`.

        Each call to `compute_and_persist` does a transactional
        delete-then-bulk_create scoped to `(symbol, lookback_hours)` —
        so the three windows don't contend, and a failure on one
        window leaves the others untouched. The reported `created`
        count is the sum across windows; the first error (if any) is
        surfaced on the consolidated `SourceResult`. Idempotent —
        replays of the same refresh produce the same persisted set.
        """
        label = "clusters"
        total_created = 0
        windows: list[int] = []
        first_error: str | None = None
        logger.info(
            "refresh source start: %s windows=%s",
            label,
            list(ClusterIdentifierController.SUPPORTED_LOOKBACKS),
        )
        for lb in ClusterIdentifierController.SUPPORTED_LOOKBACKS:
            try:
                summary = self._cluster_identifier.compute_and_persist(
                    symbol,
                    lookback_hours=lb,
                )
                total_created += summary["created"]
                windows.append(lb)
                logger.info(
                    "refresh source: %s lb=%dh deleted=%d created=%d anchor=%s",
                    label,
                    lb,
                    summary["deleted"],
                    summary["created"],
                    summary["anchor_price"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("refresh: %s lb=%dh failed", label, lb)
                if first_error is None:
                    first_error = f"lb={lb}h {type(exc).__name__}: {exc}"
        logger.info(
            "refresh source done: %s windows=%s total_created=%d",
            label,
            windows,
            total_created,
        )
        return SourceResult(
            label=label,
            backfilled=False,  # full replace; not a fetch
            received=0,
            created=total_created,
            updated=0,
            error=first_error,
        )

    # ---- internals — helpers -----------------------------------------------
    def _validate(self, symbol: str, interval: str) -> tuple[str, str]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if interval != self.DECISION_INTERVAL:
            # The chart-view UI disables the button off-15m, but a direct
            # POST could still arrive — so reject server-side too.
            raise ValueError(
                f"refresh is only available at {self.DECISION_INTERVAL} " f"(got {interval!r})"
            )
        return normalized_symbol, interval

    def _covers_lookback(self, recent_count: int, expected_recent: float) -> bool:
        """True iff the lookback window is densely enough populated to
        skip the backfill.

        The caller passes:
          * `recent_count` — rows for this (symbol, source) whose
            timestamp falls in `[now − LOOKBACK_DAYS, now]`.
          * `expected_recent` — what that count would be at the source's
            native cadence (e.g. 365 × 288 for 5m over a year).

        We require `recent_count / expected_recent ≥ DENSITY_THRESHOLD`.
        Density subsumes the older earliest/latest pair of checks:
          * a window that doesn't reach back a full year → recent rows
            are missing on the old end → density falls below threshold;
          * a stale tail (latest is weeks/months old) → recent rows
            are missing on the new end → density falls below threshold;
          * an orphan-island state (one stray ancient row outside the
            window + a thin recent tail) → recent count is tiny → density
            falls far below threshold and the backfill fires.

        One row counts as covered without exceptions — even a single
        `expected_recent < 1` source (none today, but future-proof) would
        return True iff the row is present.
        """
        if expected_recent <= 0:
            return False
        return (recent_count / expected_recent) >= self.DENSITY_THRESHOLD

    def _lookback_start(self) -> datetime:
        """Start of the lookback window — the lower bound on "recent" rows."""
        return self._now() - timedelta(days=self.LOOKBACK_DAYS)

    @staticmethod
    def _now() -> datetime:
        """Single source of "now" so a test can patch one method."""
        return datetime.now(UTC)
