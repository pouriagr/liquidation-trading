"""The `Candle` model: one OHLCV bar for a (symbol, interval) pair.

See `docs/liquidation_framework_concept.md` for why candles are the
foundational (Tier 1) data source for this project.
"""

from django.db import models

from data.models.choices import Interval, Symbol


class Candle(models.Model):
    """A single OHLCV candle for a (symbol, interval) pair.

    Field layout mirrors the Binance futures kline payload one-to-one so the
    controller can populate it with a direct mapping. The natural key is
    (symbol, interval, open_time) which lets `update_or_create` make repeat
    fetches idempotent — re-running the fetch updates the still-open last
    candle in place rather than creating duplicates.
    """

    symbol = models.CharField(
        max_length=20,
        choices=Symbol.choices,
    )
    # 15m is the framework's recommended operating timeframe — see
    # docs/liquidation_framework_concept.md.
    interval = models.CharField(
        max_length=5,
        choices=Interval.choices,
    )

    open_time = models.DateTimeField()
    close_time = models.DateTimeField()

    # Crypto-grade precision: Decimal, never float. 8 decimal places matches
    # Binance's wire format; 20 total digits comfortably covers any quoted
    # price (BTC at $1m would be 7 integer digits).
    open = models.DecimalField(max_digits=20, decimal_places=8)
    high = models.DecimalField(max_digits=20, decimal_places=8)
    low = models.DecimalField(max_digits=20, decimal_places=8)
    close = models.DecimalField(max_digits=20, decimal_places=8)

    # Volumes can be much larger than prices (millions of base units on liquid
    # pairs), so widen the integer side to 22 digits.
    volume = models.DecimalField(max_digits=30, decimal_places=8)
    quote_volume = models.DecimalField(max_digits=30, decimal_places=8)

    trades = models.PositiveIntegerField()

    # Taker-buy volumes are used later as a CVD proxy (see framework doc,
    # "Cumulative Volume Delta" section).
    taker_buy_base_volume = models.DecimalField(max_digits=30, decimal_places=8)
    taker_buy_quote_volume = models.DecimalField(max_digits=30, decimal_places=8)

    # Per-bar volume delta = 2 × taker_buy_base_volume − volume. Populated
    # automatically by a `pre_save` signal in the `feature` app
    # (`feature.signals.set_candle_delta`) so the formula lives in one
    # place and `data` stays a pure ingestion layer. CVD is the running
    # sum of this column over a fixed window — see
    # `feature.controllers.cvd.CVDController`.
    #
    # Nullable on purpose, permanently: a row written before the
    # `feature` app was installed (or any future partial save) gets
    # `NULL` here, and `CVDController` treats NULL as a window gap.
    delta = models.DecimalField(max_digits=30, decimal_places=8, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-open_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "interval", "open_time"],
                name="uniq_candle",
            ),
        ]
        indexes = [
            models.Index(
                fields=["symbol", "interval", "-open_time"],
                name="candle_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} {self.interval} @ {self.open_time:%Y-%m-%d %H:%M} UTC"
