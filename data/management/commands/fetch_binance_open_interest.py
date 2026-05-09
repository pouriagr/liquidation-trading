"""Management command: fetch the last N Binance futures OI buckets.

Logic-free wrapper — all real work lives in
`data.controllers.binance_open_interest.BinanceOpenInterestController`. This
command only parses argv and prints a one-line result.

Usage:
    poetry run python manage.py fetch_binance_open_interest BTCUSDT \
        --period 5m --limit 500
"""

from django.core.management.base import BaseCommand

from data.controllers import binance_open_interest_controller


class Command(BaseCommand):
    help = "Fetch the last N Binance USDT-M futures OI buckets and upsert them."

    def add_arguments(self, parser):
        parser.add_argument("symbol", nargs="?", default="BTCUSDT")
        parser.add_argument("--period", default="5m")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **opts):
        result = binance_open_interest_controller.fetch_and_store(
            symbol=opts["symbol"],
            period=opts["period"],
            limit=opts["limit"],
        )
        self.stdout.write(
            f"[fetch] {result.symbol} {result.period}  "
            f"received={result.received}  "
            f"created={result.created}  updated={result.updated}"
        )


# poetry run python manage.py fetch_binance_open_interest \
#   --symbol BTCUSDT --period 5m --limit 500
