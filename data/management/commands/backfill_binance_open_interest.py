"""Management command: backfill OI history from the Binance metrics archive.

Logic-free wrapper — all real work lives in
`data.controllers.binance_metrics_archive.BinanceMetricsArchiveController`.
This command only parses argv and prints a one-line result.

The archive at `data.binance.vision` extends OI history far beyond the
~30-day window served by the live `/futures/data/openInterestHist` endpoint
(see `docs/liquidation_framework_concept.md` Section 11.1). Rows are
upserted into the same `OpenInterest` table the live fetch writes to, so
the live tail and the historical depth share one model.

Usage:
    poetry run python manage.py backfill_binance_open_interest BTCUSDT \
        --start 2024-01-01 --end 2024-01-07
"""

from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand

from data.controllers import binance_metrics_archive_controller


def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


class Command(BaseCommand):
    help = "Backfill Binance USDT-M OI history from the public metrics archive."

    def add_arguments(self, parser):
        parser.add_argument("symbol", nargs="?", default="BTCUSDT")
        parser.add_argument(
            "--start",
            type=_parse_date,
            default=None,
            help="Inclusive start date (YYYY-MM-DD). Default: end - 7 days.",
        )
        parser.add_argument(
            "--end",
            type=_parse_date,
            default=None,
            help="Inclusive end date (YYYY-MM-DD). Default: yesterday UTC.",
        )

    def handle(self, *args, **opts):
        # Defaults are computed here (not in add_arguments) so they reflect
        # the actual run time, not import time.
        yesterday_utc = (datetime.now(UTC) - timedelta(days=1)).date()
        end = opts["end"] or yesterday_utc
        start = opts["start"] or (end - timedelta(days=7))

        result = binance_metrics_archive_controller.backfill(
            symbol=opts["symbol"],
            start=start,
            end=end,
        )
        self.stdout.write(
            f"[backfill] {result.symbol} 5m  "
            f"range={result.start_date}..{result.end_date}  "
            f"days={result.days_succeeded} ok/{result.days_skipped} skip  "
            f"rows received={result.rows_received} "
            f"created={result.rows_created} updated={result.rows_updated}"
        )


# poetry run python manage.py backfill_binance_open_interest BTCUSDT \
# --start 2024-01-01 --end 2024-01-07
