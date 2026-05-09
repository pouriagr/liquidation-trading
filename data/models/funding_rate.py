"""The `FundingRate` model: one settlement payment for a (symbol, funding_time).

Funding is a Tier 1 (foundational) signal in the framework — see
`docs/liquidation_framework_concept.md` Sections 3.3, 4.2, and 9.2.
The framework treats funding as the "sentiment thermometer" that
validates or invalidates the OI×Price reading on a slower timescale,
and §9.2 specifically calls for percentile context against the trailing
30-day distribution. Persisting the full history is what makes that
percentile computation possible later.

Unlike OI, there is no `period` column. Binance dictates the settlement
cadence (typically every 8h on USDT-M perpetuals, sometimes 4h for a few
symbols), so the natural key collapses to (symbol, funding_time) — one
row per actual settlement.

Mirroring `OpenInterest`'s use of `update_or_create` on the natural key
makes repeat fetches idempotent: re-running over an overlapping range
updates existing rows in place rather than creating duplicates.
"""

from django.db import models

from data.models.choices import Symbol


class FundingRate(models.Model):
    """A single funding settlement for a Binance USDT-M perpetual.

    Field layout follows the Binance `/fapi/v1/fundingRate` payload so
    the controller can populate it with a direct mapping. The `mark_price`
    column is optional — Binance returns it on most rows, but some older
    historical entries return an empty string, which we coerce to NULL
    so ingestion stays lossless rather than fabricating zeroes.
    """

    symbol = models.CharField(
        max_length=20,
        choices=Symbol.choices,
    )

    # The settlement time as published by Binance (`fundingTime` field,
    # ms epoch), converted to UTC datetime by the controller.
    funding_time = models.DateTimeField()

    # Funding rates arrive as decimal strings such as "0.00010000" or
    # "-0.00012345". Negative values are normal. Sized for historical
    # extremes — Binance has rare-day prints near ±0.75% per settlement
    # (~7.5e-3), so 12 digits with 10 decimal places leaves headroom
    # without wasting storage.
    funding_rate = models.DecimalField(max_digits=12, decimal_places=10)

    # Mark price at settlement, in quote-asset notional (USD). Nullable
    # because some older rows return an empty string; matches Candle's
    # close precision (30 digits, 8 dp) for cross-checking.
    mark_price = models.DecimalField(
        max_digits=30,
        decimal_places=8,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-funding_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "funding_time"],
                name="uniq_funding_rate",
            ),
        ]
        indexes = [
            models.Index(
                fields=["symbol", "-funding_time"],
                name="fr_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} funding @ {self.funding_time:%Y-%m-%d %H:%M} UTC"
