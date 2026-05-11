"""Derive 1h OpenInterest rows from the 5m rows already in the DB.

The 5m OI period is what Binance publishes (live API + metrics archive).
1h is what the framework's cluster-identification analysis wants
(`docs/liquidation_framework_concept.md` §12.2). Instead of fetching
1h OI from a second source — which doesn't exist in the public archive
— we aggregate the 5m rows we already have.

This controller is the DB-I/O wrapper around the pure
`feature.services.oi.aggregate_5m_to_1h` helper. Splitting the math
from the persistence keeps the math testable without a database, and
keeps the controller small enough to read top to bottom.

Idempotent: re-running over the same window upserts each 1h row in
place via the `(symbol, period, timestamp)` natural key on
`OpenInterest`. Re-deriving after new 5m rows arrive simply re-emits
the affected hour buckets — the trailing in-progress hour gets its
"current close" rewritten until the hour finishes.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.db import transaction

from data.models import OpenInterest, Symbol
from feature.services.oi import aggregate_5m_to_1h, hour_floor

logger = logging.getLogger(__name__)


@dataclass
class AggregationResult:
    """Summary returned by `OIAggregatorController.aggregate`."""

    symbol: str
    rows_read_5m: int
    rows_written_1h: int
    rows_created: int
    rows_updated: int
    start: datetime | None
    end: datetime | None


class OIAggregatorController:
    """Build `OpenInterest(period='1h')` rows from `period='5m'` rows."""

    SOURCE_PERIOD = "5m"
    TARGET_PERIOD = "1h"
    ALLOWED_SYMBOLS = frozenset(Symbol.values)

    # ---- public entry point -------------------------------------------------
    def aggregate(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> AggregationResult:
        """Aggregate 5m → 1h for `symbol` over the (optional) window.

        With `start`/`end` omitted, the full available 5m history for
        the symbol is rolled up. Both bounds, when supplied, are
        snapped to whole-hour boundaries so a half-hour query window
        doesn't produce a half-hour bucket. The bounds are inclusive
        on `start`, exclusive on `end`.
        """
        symbol = self._validate(symbol, start, end)

        # Snap to hour boundaries — see docstring.
        if start is not None:
            start = hour_floor(start)
        # `end` is exclusive; we leave it as-is and use `__lt` below.

        qs = OpenInterest.objects.filter(symbol=symbol, period=self.SOURCE_PERIOD)
        if start is not None:
            qs = qs.filter(timestamp__gte=start)
        if end is not None:
            qs = qs.filter(timestamp__lt=end)

        rows_5m = list(
            qs.order_by("timestamp").values_list(
                "timestamp", "sum_open_interest", "sum_open_interest_value"
            )
        )
        if not rows_5m:
            return AggregationResult(
                symbol=symbol,
                rows_read_5m=0,
                rows_written_1h=0,
                rows_created=0,
                rows_updated=0,
                start=None,
                end=None,
            )

        # `aggregate_5m_to_1h` expects plain tuples — the queryset
        # already produces those via `values_list`.
        hourly = aggregate_5m_to_1h(rows_5m)
        created, updated = self._persist(symbol, hourly)

        return AggregationResult(
            symbol=symbol,
            rows_read_5m=len(rows_5m),
            rows_written_1h=len(hourly),
            rows_created=created,
            rows_updated=updated,
            start=hourly[0].timestamp if hourly else None,
            end=hourly[-1].timestamp if hourly else None,
        )

    # ---- internals ----------------------------------------------------------
    def _validate(self, symbol: str, start: datetime | None, end: datetime | None) -> str:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        for label, value in (("start", start), ("end", end)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{label} must be a timezone-aware datetime")
        if start is not None and end is not None and start >= end:
            raise ValueError("start must be < end")
        return normalized_symbol

    @transaction.atomic
    def _persist(self, symbol: str, hourly) -> tuple[int, int]:
        """Upsert 1h rows. Returns (created, updated).

        Same row-by-row `update_or_create` pattern as the other
        controllers (see `data.controllers.binance_open_interest`); the
        row counts we'll write are at most ~8.7k per year of 5m data,
        which is comfortable for this approach.
        """
        created = 0
        updated = 0
        for row in hourly:
            _, was_created = OpenInterest.objects.update_or_create(
                symbol=symbol,
                period=self.TARGET_PERIOD,
                timestamp=row.timestamp,
                defaults={
                    "sum_open_interest": row.sum_open_interest,
                    "sum_open_interest_value": row.sum_open_interest_value,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated

    # ---- convenience --------------------------------------------------------
    @staticmethod
    def now_utc() -> datetime:
        """Single source of truth for "now" in this controller — overridable in tests."""
        return datetime.now(UTC)

    def aggregate_recent(self, symbol: str, days: int) -> AggregationResult:
        """Roll up only the most recent `days` of 5m rows.

        Used by `RefreshController` to avoid re-aggregating multi-year
        history on every refresh once the table is primed.
        """
        if days < 1:
            raise ValueError("days must be >= 1")
        start = hour_floor(self.now_utc() - timedelta(days=days))
        return self.aggregate(symbol=symbol, start=start)
