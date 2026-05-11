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

  1. Check the earliest timestamp already in the DB for that source.
  2. If it covers `LOOKBACK_DAYS` (1 year) — call only the live `fetch_*`
     controller to top up the tail. Cheap, fast, idempotent.
  3. Otherwise — call the matching `backfill_*` controller first, *then*
     the live fetch. The archive controllers cover history up to a
     recent boundary; the live fetch closes the small gap between that
     boundary and "now".

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

from django.db.models import Min

from data.controllers import (
    binance_candles_controller,
    binance_funding_rate_controller,
    binance_klines_archive_controller,
    binance_metrics_archive_controller,
    binance_open_interest_controller,
)
from data.models import Candle, FundingRate, OpenInterest, Symbol
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

    def __init__(self, oi_aggregator: OIAggregatorController | None = None) -> None:
        # Injected so a test can substitute a stub aggregator without
        # patching at the import level. Defaults to a real one because
        # production callers don't need to know it exists.
        self._oi_aggregator = oi_aggregator or OIAggregatorController()

    # ---- public entry point -------------------------------------------------
    def refresh(self, symbol: str, interval: str) -> RefreshResult:
        """Bring the full 15m bundle up to date for `symbol`.

        `interval` must equal `DECISION_INTERVAL` ("15m"). It's accepted
        as a parameter (rather than ignored) so callers — the chart view
        — can pass through the user's selected interval verbatim and let
        validation reject non-15m calls with a clean error.
        """
        symbol, interval = self._validate(symbol, interval)

        result = RefreshResult(symbol=symbol, decision_interval=interval)

        for tf in self.CANDLE_INTERVALS:
            result.sources.append(self._refresh_candles(symbol, tf))

        for period in self.OI_PERIODS:
            result.sources.append(self._refresh_oi(symbol, period))

        result.sources.append(self._refresh_funding(symbol))

        # Derive 1h OI from the 5m rows we just (re)freshed. Done last so
        # any per-source failure on the 5m OI step shows up explicitly
        # — and the derived row makes it clear when the input wasn't
        # actually refreshed (zero received → zero created/updated).
        result.sources.append(self._derive_oi_1h(symbol))

        return result

    # ---- internals — per-source orchestration -------------------------------
    def _refresh_candles(self, symbol: str, interval: str) -> SourceResult:
        label = f"candles {interval}"
        try:
            earliest = Candle.objects.filter(symbol=symbol, interval=interval).aggregate(
                m=Min("open_time")
            )["m"]
            backfilled = False
            if not self._has_year(earliest):
                binance_klines_archive_controller.backfill(
                    symbol=symbol, interval=interval, months=12
                )
                backfilled = True
            fetch = binance_candles_controller.fetch_and_store(
                symbol=symbol, interval=interval, limit=self.CANDLE_FETCH_LIMIT
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
            earliest = OpenInterest.objects.filter(symbol=symbol, period=period).aggregate(
                m=Min("timestamp")
            )["m"]
            backfilled = False
            if not self._has_year(earliest):
                today = self._now().date()
                start = today - timedelta(days=self.LOOKBACK_DAYS)
                end = today - timedelta(days=1)  # archive controller requires end < today
                binance_metrics_archive_controller.backfill(symbol=symbol, start=start, end=end)
                backfilled = True
            fetch = binance_open_interest_controller.fetch_and_store(
                symbol=symbol, period=period, limit=self.OI_FETCH_LIMIT
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
            earliest = FundingRate.objects.filter(symbol=symbol).aggregate(m=Min("funding_time"))[
                "m"
            ]
            # FundingRate has no dedicated backfill controller — the live
            # fetch endpoint already serves full history when given a
            # start_time, and the controller pages internally via _paginate.
            # So "backfill" here is the same controller called in range mode.
            if not self._has_year(earliest):
                start_time = self._now() - timedelta(days=self.LOOKBACK_DAYS)
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
            res = self._oi_aggregator.aggregate_recent(symbol=symbol, days=self.LOOKBACK_DAYS)
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

    def _has_year(self, earliest: datetime | None) -> bool:
        """True iff the DB already covers `LOOKBACK_DAYS` for this source."""
        if earliest is None:
            return False
        return earliest <= self._now() - timedelta(days=self.LOOKBACK_DAYS)

    @staticmethod
    def _now() -> datetime:
        """Single source of "now" so a test can patch one method."""
        return datetime.now(UTC)
