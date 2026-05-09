"""Management command: backfill candle history from the Binance klines archive.

Logic-free wrapper — all real work lives in
`data.controllers.binance_klines_archive.BinanceKlinesArchiveController`.
This command only parses argv and prints a one-line result.

The archive at `data.binance.vision` extends candle history far beyond the
~25h–15d window served by the live `/fapi/v1/klines` endpoint (capped at
`limit=1500`). Rows are upserted into the same `Candle` table the live
fetch writes to, so the live tail and the historical depth share one
model — same as the OI archive backfill alongside this command.

Usage:
    poetry run python manage.py backfill_binance_candles BTCUSDT \
        --interval 15m --months 12
"""

from django.core.management.base import BaseCommand

from data.controllers import binance_klines_archive_controller


class Command(BaseCommand):
    help = "Backfill Binance USDT-M candles from the public klines archive."

    def add_arguments(self, parser):
        parser.add_argument("symbol", nargs="?", default="BTCUSDT")
        parser.add_argument("--interval", default="15m")
        parser.add_argument(
            "--months",
            type=int,
            default=12,
            help="Number of most-recent closed calendar months to backfill (default: 12).",
        )

    def handle(self, *args, **opts):
        result = binance_klines_archive_controller.backfill(
            symbol=opts["symbol"],
            interval=opts["interval"],
            months=opts["months"],
        )
        self.stdout.write(
            f"[backfill] {result.symbol} {result.interval}  "
            f"range={result.start_month}..{result.end_month}  "
            f"months={result.months_succeeded} ok/{result.months_skipped} skip  "
            f"days={result.days_succeeded} ok/{result.days_skipped} skip  "
            f"rows received={result.rows_received} "
            f"created={result.rows_created} updated={result.rows_updated}"
        )


# poetry run python manage.py backfill_binance_candles BTCUSDT \
# --interval 15m --months 12
