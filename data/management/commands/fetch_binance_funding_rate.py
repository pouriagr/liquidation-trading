"""Management command: fetch Binance futures funding-rate settlements.

Logic-free wrapper — all real work lives in
`data.controllers.binance_funding_rate.BinanceFundingRateController`.
This command only parses argv and prints a one-line result.

Two modes share one command:

  - **Latest mode** (default): pass `--limit N` to upsert the most recent
    N settlements for the symbol. Default limit is 1000 (Binance's hard
    cap), since funding settles only every 4-8h, so 1000 rows is roughly
    a year of history per call — typically enough for a fresh start.

  - **Range mode**: pass `--start` (and optionally `--end`) to backfill an
    arbitrary window. The controller paginates in `--limit`-row batches
    until the window is covered. The same idempotent upsert applies, so
    overlapping reruns are safe.

Both `--start` and `--end` accept either a calendar date `YYYY-MM-DD`
(interpreted as UTC midnight) or a full ISO 8601 datetime. Naive inputs
are assumed to be UTC.

Usage:
    poetry run python manage.py fetch_binance_funding_rate BTCUSDT \
        --limit 1000

    poetry run python manage.py fetch_binance_funding_rate BTCUSDT \
        --start 2024-01-01 --end 2024-04-01
"""

from datetime import UTC, datetime

from django.core.management.base import BaseCommand

from data.controllers import binance_funding_rate_controller


def _parse_iso_utc(s: str) -> datetime:
    """Parse YYYY-MM-DD or full ISO datetime into a tz-aware UTC datetime.

    `datetime.fromisoformat` accepts both shapes (`2024-01-01` and
    `2024-01-01T08:00:00`, with or without timezone). Naive results are
    pinned to UTC; aware results are converted to UTC so downstream
    `_to_ms` is unambiguous.
    """
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class Command(BaseCommand):
    help = "Fetch Binance USDT-M futures funding-rate settlements and upsert them."

    def add_arguments(self, parser):
        parser.add_argument("symbol", nargs="?", default="BTCUSDT")
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument(
            "--start",
            type=_parse_iso_utc,
            default=None,
            help="Inclusive UTC start (YYYY-MM-DD or ISO). Triggers range mode.",
        )
        parser.add_argument(
            "--end",
            type=_parse_iso_utc,
            default=None,
            help="Inclusive UTC end (YYYY-MM-DD or ISO). Optional — when "
            "omitted with --start, the controller pages forward to 'now'.",
        )

    def handle(self, *args, **opts):
        result = binance_funding_rate_controller.fetch_and_store(
            symbol=opts["symbol"],
            limit=opts["limit"],
            start_time=opts["start"],
            end_time=opts["end"],
        )
        self.stdout.write(
            f"[fetch] {result.symbol}  "
            f"received={result.received}  "
            f"created={result.created}  updated={result.updated}"
        )


# poetry run python manage.py fetch_binance_funding_rate BTCUSDT --limit 1000
# poetry run python manage.py fetch_binance_funding_rate BTCUSDT \
#   --start 2024-01-01 --end 2024-04-01
