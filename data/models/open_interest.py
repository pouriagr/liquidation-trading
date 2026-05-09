"""The `OpenInterest` model: one OI snapshot for a (symbol, period, timestamp).

OI is a Tier 1 (foundational) signal in the framework — see
`docs/liquidation_framework_concept.md` Sections 3.2 and 11.5. The framework
operates on 5-minute resolution for OI delta tracking (Section 12.2); that's
also Binance's finest available resolution on the public endpoint
(`/futures/data/openInterestHist`, Section 4.3). Coarser periods (1h for
cluster identification) are accepted by the schema and the controller, so
extending later is no-code-change.

The natural key is (symbol, period, timestamp), mirroring `Candle`'s
(symbol, interval, open_time) so `update_or_create` makes repeat fetches
idempotent.
"""

from django.db import models

from data.models.choices import Interval, Symbol


class OpenInterest(models.Model):
    """A single open-interest snapshot for a (symbol, period) bucket.

    Field layout mirrors the Binance `openInterestHist` payload one-to-one
    so the controller can populate it with a direct mapping. Both the
    base-coin amount (`sum_open_interest`, e.g. BTC units) and the USD
    notional (`sum_open_interest_value`) are persisted — the framework
    references the notional value for cluster strength analysis, while the
    base amount is useful for coin-denominated derivations.
    """

    symbol = models.CharField(
        max_length=20,
        choices=Symbol.choices,
    )
    # Reuses the Interval enum as the source of choices for parity with
    # `Candle.interval`. The OI endpoint accepts a smaller subset
    # (no 1m/3m/8h/3d/1w/1M); the controller's ALLOWED_PERIODS is the
    # authoritative gate at fetch time.
    period = models.CharField(
        max_length=5,
        choices=Interval.choices,
    )

    # The bucket time as published by Binance (`timestamp` field, ms epoch),
    # converted to UTC datetime by the controller.
    timestamp = models.DateTimeField()

    # Crypto-grade precision: Decimal, never float. Match Candle's volume
    # precision (30 digits, 8 dp) — both fields can be very large notional
    # numbers (USD value can run into the billions on BTC).
    sum_open_interest = models.DecimalField(max_digits=30, decimal_places=8)
    sum_open_interest_value = models.DecimalField(max_digits=30, decimal_places=8)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "period", "timestamp"],
                name="uniq_open_interest",
            ),
        ]
        indexes = [
            models.Index(
                fields=["symbol", "period", "-timestamp"],
                name="oi_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} {self.period} OI @ {self.timestamp:%Y-%m-%d %H:%M} UTC"
