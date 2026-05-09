"""Management command: fetch the last N Binance futures candles.

Logic-free wrapper — all real work lives in
`data.controllers.binance_candles.BinanceCandlesController`. This command only
parses argv and prints a one-line result.

Usage:
    poetry run python manage.py fetch_binance_candles BTCUSDT \
        --interval 15m --limit 500
"""

from django.core.management.base import BaseCommand

from data.controllers import binance_candles_controller


class Command(BaseCommand):
    help = "Fetch the last N Binance USDT-M futures candles and upsert them."

    def add_arguments(self, parser):
        parser.add_argument("symbol", nargs="?", default="BTCUSDT")
        parser.add_argument("--interval", default="15m")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **opts):
        result = binance_candles_controller.fetch_and_store(
            symbol=opts["symbol"],
            interval=opts["interval"],
            limit=opts["limit"],
        )
        self.stdout.write(
            f"[fetch] {result.symbol} {result.interval}  "
            f"received={result.received}  "
            f"created={result.created}  updated={result.updated}"
        )


# poetry run python manage.py fetch_binance_candles BTCUSDT --interval 15m --limit 500
