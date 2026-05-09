"""Enumerations used by the data app's models.

Kept separate from the model definitions so they can be imported without
pulling the full ORM machinery and so the model files stay focused on schema.
"""

from django.db import models


class Symbol(models.TextChoices):
    """Binance USDT-M futures symbols this project tracks.

    Starter set of the most liquid USDT-perp pairs. Extend as needed — the
    controller validates submitted symbols against this list.
    """

    BTCUSDT = "BTCUSDT", "BTC / USDT"
    ETHUSDT = "ETHUSDT", "ETH / USDT"
    BNBUSDT = "BNBUSDT", "BNB / USDT"
    SOLUSDT = "SOLUSDT", "SOL / USDT"
    XRPUSDT = "XRPUSDT", "XRP / USDT"
    DOGEUSDT = "DOGEUSDT", "DOGE / USDT"
    ADAUSDT = "ADAUSDT", "ADA / USDT"
    AVAXUSDT = "AVAXUSDT", "AVAX / USDT"
    LINKUSDT = "LINKUSDT", "LINK / USDT"
    TRXUSDT = "TRXUSDT", "TRX / USDT"


class Interval(models.TextChoices):
    """Binance kline intervals.

    Values are the exact strings Binance accepts as the `interval` query
    param on `/fapi/v1/klines`. Member names are uppercased Python
    identifiers — note that `MIN_1` ("1m") and `MONTH_1` ("1M") share a
    case-insensitive form, which is why we suffix with the unit name.
    """

    MIN_1 = "1m", "1 minute"
    MIN_3 = "3m", "3 minutes"
    MIN_5 = "5m", "5 minutes"
    MIN_15 = "15m", "15 minutes"
    MIN_30 = "30m", "30 minutes"
    HOUR_1 = "1h", "1 hour"
    HOUR_2 = "2h", "2 hours"
    HOUR_4 = "4h", "4 hours"
    HOUR_6 = "6h", "6 hours"
    HOUR_8 = "8h", "8 hours"
    HOUR_12 = "12h", "12 hours"
    DAY_1 = "1d", "1 day"
    DAY_3 = "3d", "3 days"
    WEEK_1 = "1w", "1 week"
    MONTH_1 = "1M", "1 month"
