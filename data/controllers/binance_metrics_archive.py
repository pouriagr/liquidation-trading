"""Controller for backfilling OI from the Binance public metrics archive.

The live `BinanceOpenInterestController` (see `binance_open_interest.py`)
covers only the most recent ~30 days because that is all the
`/futures/data/openInterestHist` endpoint serves. This controller fills the
historical gap by reading per-day ZIPs from the public S3 archive at
`data.binance.vision`. It writes into the same `OpenInterest` table, keyed
on the same natural key (symbol, period, timestamp), so the live tail and
the historical depth are seamless after a successful backfill.

Source URL pattern (no API key, no rate limit per the framework doc 11.2):

    https://data.binance.vision/data/futures/um/daily/metrics/{SYMBOL}/{SYMBOL}-metrics-{YYYY-MM-DD}.zip

Each ZIP contains a single CSV with header:

    create_time, symbol, sum_open_interest, sum_open_interest_value,
    count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
    count_long_short_ratio, sum_taker_long_short_vol_ratio

We persist only the two OI columns. The four LSR columns are intentionally
discarded — we will add them in a sibling `LongShortRatio` model when the
matching live LSR endpoints get wired up, so live and historical LSR land
together.

A note on the framework doc: Section 11.2 currently states "the single
absence [in the public archive] is open interest history." That claim is
out of date; the metrics archive used here closes exactly that gap.
"""

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import requests
from django.db import transaction

from data.models import OpenInterest, Symbol

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    """Summary returned by `BinanceMetricsArchiveController.backfill`."""

    symbol: str
    start_date: date
    end_date: date
    days_attempted: int
    days_skipped: int  # 404 / not yet published / pair didn't exist that day
    days_succeeded: int
    rows_received: int
    rows_created: int
    rows_updated: int


class BinanceMetricsArchiveController:
    """Backfills OI history from the public per-day metrics archive."""

    BASE_URL = (
        "https://data.binance.vision/data/futures/um/daily/metrics/"
        "{symbol}/{symbol}-metrics-{date}.zip"
    )
    REQUEST_TIMEOUT = 30  # ZIPs are larger than JSON; bigger budget than live
    PERIOD = "5m"  # Archive's native resolution; mirrors the live OI controller
    ALLOWED_SYMBOLS = frozenset(Symbol.values)
    # Soft sanity check on rows-per-day. 5-min × 24h = 288. We warn if a fetched
    # day deviates far from this — a count of, say, 1 would mean the archive is
    # actually daily-aggregated, which would invalidate the schema assumption.
    EXPECTED_ROWS_PER_DAY = 288

    # ---- public entry point -------------------------------------------------
    def backfill(self, symbol: str, start: date, end: date) -> BackfillResult:
        """Backfill OI buckets for `symbol` over the inclusive date range.

        Idempotent: re-running with the same args updates rows in place via
        the (symbol, period, timestamp) unique constraint on `OpenInterest`.
        Per-day 404s are logged and counted as skipped, not raised.
        """
        symbol, start, end = self._validate(symbol, start, end)

        days_attempted = 0
        days_skipped = 0
        days_succeeded = 0
        rows_received = 0
        rows_created = 0
        rows_updated = 0

        day = start
        while day <= end:
            days_attempted += 1
            rows = self._fetch_day(symbol, day)
            if rows is None:
                days_skipped += 1
            else:
                self._sanity_check(symbol, day, rows)
                created, updated = self._persist_day(symbol, rows)
                days_succeeded += 1
                rows_received += len(rows)
                rows_created += created
                rows_updated += updated
            day += timedelta(days=1)

        return BackfillResult(
            symbol=symbol,
            start_date=start,
            end_date=end,
            days_attempted=days_attempted,
            days_skipped=days_skipped,
            days_succeeded=days_succeeded,
            rows_received=rows_received,
            rows_created=rows_created,
            rows_updated=rows_updated,
        )

    # ---- internals ----------------------------------------------------------
    def _validate(self, symbol: str, start: date, end: date) -> tuple[str, date, date]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if not isinstance(start, date) or not isinstance(end, date):
            raise ValueError("start and end must be date objects")
        if start > end:
            raise ValueError("start must be <= end")
        today_utc = datetime.now(UTC).date()
        if end >= today_utc:
            raise ValueError("end must be before today UTC (the file is not yet published)")
        return normalized_symbol, start, end

    def _fetch_day(self, symbol: str, day: date) -> list[dict] | None:
        """Download and parse one day's metrics ZIP.

        Returns a list of CSV rows as dicts, or None if the file is not
        available (HTTP 404). Any other HTTP/network error is raised.
        """
        url = self.BASE_URL.format(symbol=symbol, date=day.isoformat())
        resp = requests.get(url, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 404:
            logger.info("metrics archive 404: %s %s", symbol, day.isoformat())
            return None
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            members = zf.namelist()
            if not members:
                raise ValueError(f"empty archive for {symbol} {day}")
            # Pick the single member regardless of its filename — robust to
            # any future name changes.
            with zf.open(members[0]) as f:
                text = io.TextIOWrapper(f, encoding="utf-8", newline="")
                return list(csv.DictReader(text))

    def _sanity_check(self, symbol: str, day: date, rows: list[dict]) -> None:
        """Warn (don't raise) if rows-per-day is far from the 288 expectation."""
        n = len(rows)
        if n < 200 or n > 300:
            logger.warning(
                "metrics row count off for %s %s: got %d (expected ~%d)",
                symbol,
                day.isoformat(),
                n,
                self.EXPECTED_ROWS_PER_DAY,
            )

    @staticmethod
    def _row_to_defaults(row: dict) -> dict:
        """Map an archive CSV row to OpenInterest field defaults.

        Only the OI fields are persisted; the four LSR columns present in
        the CSV (count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
        count_long_short_ratio, sum_taker_long_short_vol_ratio) are
        intentionally discarded — see this module's docstring.
        """
        return {
            # Build Decimal from the raw CSV string — going via float would
            # silently lose digits. Same idiom as binance_open_interest.py.
            "sum_open_interest": Decimal(str(row["sum_open_interest"])),
            "sum_open_interest_value": Decimal(str(row["sum_open_interest_value"])),
        }

    @staticmethod
    def _parse_create_time(s: str) -> datetime:
        """Parse the archive's `create_time` column into a UTC datetime.

        Binance's metrics CSVs use a UTC datetime string. The canonical
        format is "YYYY-MM-DD HH:MM:SS"; a couple of close variants are
        tolerated so a small upstream format drift doesn't kill the run.
        """
        s = s.strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        raise ValueError(f"unrecognized create_time format: {s!r}")

    @transaction.atomic
    def _persist_day(self, symbol: str, rows: list[dict]) -> tuple[int, int]:
        created = 0
        updated = 0
        for row in rows:
            timestamp = self._parse_create_time(row["create_time"])
            _, was_created = OpenInterest.objects.update_or_create(
                symbol=symbol,
                period=self.PERIOD,
                timestamp=timestamp,
                defaults=self._row_to_defaults(row),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated
