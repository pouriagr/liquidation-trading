"""The `ClusterSegment` model: one persisted §5 liquidation band.

Companion to the in-memory `feature.services.clustering.ClusterSegment`
dataclass — same fields, same units, same semantics. The dataclass is
what the math layer emits; the ORM model is what `RefreshController`
writes to the DB so subsequent `/api/clusters/...` GETs can serve from
storage instead of recomputing.

Rows are owned by `feature.controllers.cluster_identifier.
ClusterIdentifierController.compute_and_persist`, which runs per-symbol
inside `transaction.atomic` on every refresh. The pattern is
delete-then-bulk_create: the math may re-evaluate older zones against
fresh sweep candles, so a per-row update path can't safely merge — a
full replace keeps the persisted set consistent with what `identify()`
would produce live.

No unique constraint or natural key: identity is "this (symbol,
lookback_hours) run produced this segment" and we never look up a
single row by its content. The lookup index
`(symbol, lookback_hours, -start_time)` covers both the typical
`order_by("start_time")` read pattern (Postgres can scan a `DESC`
index in either direction) and the
`DELETE WHERE symbol=? AND lookback_hours=?` step of the replace
cycle that now runs once per supported window per refresh.
"""

from django.db import models

from data.models.choices import Symbol


class ClusterSegment(models.Model):
    """One time-bounded liquidation band, persisted at refresh time.

    `end_time = NULL` means "not yet swept at the moment of the last
    refresh" — the chart paints these from `start_time` out to the
    right edge / wall-clock now. A subsequent refresh that observes a
    sweep replaces the row with one whose `end_time` is set.

    `price_low` / `price_high` are the band's anchor-grid edges (a
    0.10 %-of-anchor bucket per
    `ClusterIdentifierController.PRICE_BAND_PCT`); `price` is the band
    centre and is kept for tooltips that want a single representative
    number rather than a range.
    """

    symbol = models.CharField(
        max_length=20,
        choices=Symbol.choices,
    )
    # "long_liq" → liquidations sit below the source zone (longs get
    # flushed); "short_liq" → above (shorts get squeezed). Stored as a
    # plain CharField rather than another TextChoices enum because the
    # set is fixed at two values and not user-facing.
    side = models.CharField(max_length=10)

    # Crypto-grade precision on every price-shaped column — Decimal
    # never float — matching `Candle.{open,high,low,close}` (20 digits,
    # 8 dp). Wide enough for any quoted price including a $1M BTC.
    price_low = models.DecimalField(max_digits=20, decimal_places=8)
    price_high = models.DecimalField(max_digits=20, decimal_places=8)
    price = models.DecimalField(max_digits=20, decimal_places=8)

    # When the source accumulation hour started — i.e. the zone's
    # `open_time`. Drives the left edge of the rendered rectangle.
    start_time = models.DateTimeField()
    # First 5 m candle whose [low, high] overlapped this band after
    # `start_time`. NULL → no sweep observed inside the lookback at
    # the last refresh; the chart paints to the right edge.
    end_time = models.DateTimeField(null=True, blank=True)
    # Mirror of `start_time` today, but kept distinct so a later
    # change to the assembly logic (e.g. merging zones) doesn't
    # silently corrupt the link back to the originating accumulation
    # hour. Matches the dataclass field for parity with tooltips.
    source_open_time = models.DateTimeField()

    # The §5.5 strength score: notional × leverage_score(tier) ×
    # recency_weight(age_at_start), summed across tiers landing in
    # this band. Float because it's a unitless ranking, not money.
    strength = models.FloatField()
    # Raw notional in USD for the segment (sum of side-share notional
    # over tiers). Decimal so tooltips can show an exact figure;
    # matches `OpenInterest.sum_open_interest_value`'s precision.
    notional = models.DecimalField(max_digits=30, decimal_places=8)
    # Carried from the source zone; range [-1, 1]. Used by the
    # tooltip to show "long-heavy 78 %" without re-deriving it from
    # the source zone (which we don't persist).
    long_bias = models.FloatField()

    # Which rolling-lookback window produced this segment. The §5
    # pipeline now runs once per (symbol, lookback_hours) on every
    # refresh and persists each result tagged by its window so the
    # GET endpoint can serve any non-empty subset of
    # `ClusterIdentifierController.SUPPORTED_LOOKBACKS` on demand
    # without recompute. §12.2 prescribes the 24h–168h range; the
    # same band appearing in multiple windows is the §12.3 "multiple
    # resolutions agree" boost, realised at GET time as the §5.4
    # sum-of-contributions per (price_band, side).
    lookback_hours = models.PositiveSmallIntegerField()

    # Audit field — the moment the refresh wrote this row. Lets the
    # GET endpoint surface a "last computed at" timestamp without an
    # extra table, and lets a future debugger correlate rows with a
    # specific refresh run.
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Most-recent first by default, mirroring `OpenInterest` and
        # `Candle`. The chart actually wants ascending order for
        # rendering, so the view will re-order; this default just
        # makes admin/REPL inspections more useful.
        ordering = ["-start_time"]
        indexes = [
            # Compound covers both the per-window DELETE
            # `WHERE symbol=? AND lookback_hours=?` step of the replace
            # cycle and the GET-time
            # `WHERE symbol=? AND lookback_hours IN (...)` filter. The
            # `-start_time` trailing column preserves the
            # `order_by("start_time")` index-only path the chart relies on.
            models.Index(
                fields=["symbol", "lookback_hours", "-start_time"],
                name="cluster_segment_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        end = self.end_time.strftime("%Y-%m-%d %H:%M") if self.end_time else "alive"
        return (
            f"{self.symbol} {self.side} {self.price_low}-{self.price_high} "
            f"@ {self.start_time:%Y-%m-%d %H:%M} → {end}"
        )
