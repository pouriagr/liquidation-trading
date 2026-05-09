"""Controller for backfilling candles from the Binance public klines archive.

The live `BinanceCandlesController` (see `binance_candles.py`) only reaches
back as far as Binance's `limit=1500` cap on `/fapi/v1/klines` allows — at
1m that's ~25 hours, at 15m ~15 days. This controller fills the historical
gap by reading per-month ZIPs from the public archive at
`data.binance.vision` and writing into the same `Candle` table, keyed on
the same natural key (symbol, interval, open_time), so the live tail and
the historical depth are seamless after a successful backfill.

This module is the candle counterpart of `binance_metrics_archive.py`
(which does the same job for OI). The two controllers deliberately mirror
each other in shape — same `_validate / _fetch_X / _persist_X` rhythm,
same 404-as-skip semantics, same `BackfillResult`-style summary — so a
reader who has internalised one already understands the other.

Source URL pattern (no API key, no rate limit per the framework doc 11.2):

    https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{YYYY-MM}.zip

We use only the *monthly* archive — for any meaningful backfill window the
monthly variant is ~30× fewer HTTP fetches than the daily one, and the
recent partial-month tail is already covered by the live fetcher. Each ZIP
contains a single CSV with positional columns matching Binance's live
kline JSON one-to-one:

    open_time, open, high, low, close, volume,
    close_time, quote_volume, count,
    taker_buy_volume, taker_buy_quote_volume, ignore

Recent monthly files (2025+) ship with a header row; older ones don't. We
auto-detect by trying to parse the first cell as an int — same shape data
across all years, no date cutoff needed.
"""

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import requests
from django.db import transaction

from data.controllers.binance_candles import BinanceCandlesController
from data.models import Candle, Interval, Symbol

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    """Summary returned by `BinanceKlinesArchiveController.backfill`."""

    symbol: str
    interval: str
    start_month: str  # "YYYY-MM"
    end_month: str  # "YYYY-MM" — last *closed* month covered by the monthly phase
    months_attempted: int
    months_skipped: int  # 404 / not yet published / pair didn't exist that month
    months_succeeded: int
    # Daily phase covers the in-progress current calendar month, where the
    # monthly ZIP isn't published yet. Days for which the per-day ZIP also
    # isn't published yet (typically "today" in UTC) are counted as skipped.
    days_attempted: int
    days_skipped: int
    days_succeeded: int
    rows_received: int
    rows_created: int
    rows_updated: int


class BinanceKlinesArchiveController:
    """Backfills candle history from the public klines archive.

    Closed calendar months are pulled from the per-month archive; the
    in-progress current month is pulled from the per-day archive. Together
    they cover history end-to-end up to whatever Binance has published —
    no seam in the middle for the live fetcher to miss.
    """

    BASE_URL = (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
    )
    DAILY_BASE_URL = (
        "https://data.binance.vision/data/futures/um/daily/klines/"
        "{symbol}/{interval}/{symbol}-{interval}-{day}.zip"
    )
    REQUEST_TIMEOUT = 60  # monthly ZIPs are larger than the metrics ones; bigger budget
    MIN_MONTHS = 1
    MAX_MONTHS = 120  # 10y safety cap — guards a fat-finger `--months 10000`
    # Source of truth for allowed symbols/intervals is the choices module —
    # don't duplicate the lists here.
    ALLOWED_SYMBOLS = frozenset(Symbol.values)
    ALLOWED_INTERVALS = frozenset(Interval.values)

    # Minutes per `Interval` value, used by the row-count sanity check. Keys
    # are the Binance `interval` strings; missing keys disable the check
    # (we skip it for `1M` because monthly archives of monthly candles only
    # contain a single row anyway).
    _INTERVAL_MINUTES: dict[str, int] = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "2h": 120,
        "4h": 240,
        "6h": 360,
        "8h": 480,
        "12h": 720,
        "1d": 1440,
        "3d": 4320,
        "1w": 10080,
    }

    # ---- public entry point -------------------------------------------------
    def backfill(self, symbol: str, interval: str, months: int) -> BackfillResult:
        """Backfill candles for `symbol`/`interval` over the most recent
        `months` closed calendar months, plus every published day of the
        in-progress current month.

        Two phases share one codepath into `_persist_month`:
        - monthly phase: closed months from the per-month archive.
        - daily phase: days of the current month from the per-day archive,
          since the current month's monthly ZIP isn't published until after
          the month closes. Today's daily ZIP is usually unpublished too;
          that's a normal 404 and counts as a skipped day.

        Idempotent: re-running with the same args updates rows in place via
        the (symbol, interval, open_time) unique constraint on `Candle`.
        Per-fetch 404s are logged and counted as skipped, not raised.
        """
        symbol, interval, months = self._validate(symbol, interval, months)

        month_starts = self._month_range(months)

        months_attempted = 0
        months_skipped = 0
        months_succeeded = 0
        rows_received = 0
        rows_created = 0
        rows_updated = 0

        for month in month_starts:
            months_attempted += 1
            rows = self._fetch_month(symbol, interval, month)
            if rows is None:
                months_skipped += 1
                continue
            self._sanity_check(symbol, interval, month, rows)
            created, updated = self._persist_month(symbol, interval, rows)
            months_succeeded += 1
            rows_received += len(rows)
            rows_created += created
            rows_updated += updated

        # Daily phase: fill the in-progress current calendar month, which
        # has no monthly ZIP yet. Without this, a `--months N` run would
        # leave a multi-day seam between the latest monthly archive (last
        # day of previous month, 23:45 UTC) and whatever the live fetcher
        # caught with its limited look-back window.
        day_starts = self._current_month_days()

        days_attempted = 0
        days_skipped = 0
        days_succeeded = 0

        for day in day_starts:
            days_attempted += 1
            rows = self._fetch_day(symbol, interval, day)
            if rows is None:
                days_skipped += 1
                continue
            self._sanity_check_day(symbol, interval, day, rows)
            created, updated = self._persist_month(symbol, interval, rows)
            days_succeeded += 1
            rows_received += len(rows)
            rows_created += created
            rows_updated += updated

        return BackfillResult(
            symbol=symbol,
            interval=interval,
            start_month=month_starts[0].strftime("%Y-%m"),
            end_month=month_starts[-1].strftime("%Y-%m"),
            months_attempted=months_attempted,
            months_skipped=months_skipped,
            months_succeeded=months_succeeded,
            days_attempted=days_attempted,
            days_skipped=days_skipped,
            days_succeeded=days_succeeded,
            rows_received=rows_received,
            rows_created=rows_created,
            rows_updated=rows_updated,
        )

    # ---- internals ----------------------------------------------------------
    def _validate(self, symbol: str, interval: str, months: int) -> tuple[str, str, int]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if interval not in self.ALLOWED_INTERVALS:
            raise ValueError(f"interval must be one of {sorted(self.ALLOWED_INTERVALS)}")
        if not isinstance(months, int) or isinstance(months, bool):
            raise ValueError("months must be an int")
        if not (self.MIN_MONTHS <= months <= self.MAX_MONTHS):
            raise ValueError(f"months must be between {self.MIN_MONTHS} and {self.MAX_MONTHS}")
        return normalized_symbol, interval, months

    @staticmethod
    def _month_range(months: int) -> list[date]:
        """Return first-of-month dates for the most recent `months` *closed*
        calendar months (UTC), oldest first.

        e.g. today=2026-05-10, months=3 → [2026-02-01, 2026-03-01, 2026-04-01].

        The current calendar month is intentionally excluded here — its
        monthly ZIP isn't published yet. The current month is filled by
        the daily phase via `_current_month_days`.
        """
        today = datetime.now(UTC).date()
        # Step back from first-of-current-month by one day → land in the
        # previous month, then snap to its first day. This is the newest
        # *closed* month.
        newest = (today.replace(day=1) - timedelta(days=1)).replace(day=1)

        result: list[date] = [newest]
        cur = newest
        for _ in range(months - 1):
            cur = (cur - timedelta(days=1)).replace(day=1)
            result.append(cur)
        result.reverse()
        return result

    @staticmethod
    def _current_month_days() -> list[date]:
        """Return every day from the 1st of the current UTC month through
        today (inclusive), oldest first.

        Today and (often) yesterday's daily ZIPs aren't published yet;
        those 404s are absorbed by `_fetch_day` and counted as skipped
        days. We still attempt them so a re-run picks them up the moment
        they appear, without needing to know Binance's exact publish lag.
        """
        today = datetime.now(UTC).date()
        first = today.replace(day=1)
        out: list[date] = []
        cur = first
        while cur <= today:
            out.append(cur)
            cur += timedelta(days=1)
        return out

    def _fetch_zip_rows(self, url: str, label: str) -> list[list[str]] | None:
        """Download and parse one klines ZIP at `url`.

        Shared between the monthly and daily archive paths — same on-disk
        layout (one CSV per ZIP, optional header row), only the URL and
        log label differ. `label` is used in 404 / empty-archive messages
        so the caller doesn't have to format anything itself.

        Returns a list of positional CSV rows (each row a list of strings),
        or None if the file is not available (HTTP 404). Any other
        HTTP/network error is raised. The header row, if present, is
        stripped here so the caller can index every row positionally.
        """
        resp = requests.get(url, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 404:
            logger.info("klines archive 404: %s", label)
            return None
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            members = zf.namelist()
            if not members:
                raise ValueError(f"empty archive: {label}")
            # Pick the single member regardless of its filename — robust to
            # any future name changes.
            with zf.open(members[0]) as f:
                text = io.TextIOWrapper(f, encoding="utf-8", newline="")
                rows = list(csv.reader(text))

        # Auto-detect header: try to parse cell 0 of row 0 as an int (ms
        # epoch). If it raises, that row is the header — drop it. Recent
        # archive files (2025+) ship with one; older years don't.
        if rows:
            try:
                int(rows[0][0])
            except (ValueError, IndexError):
                rows = rows[1:]
        return rows

    def _fetch_month(self, symbol: str, interval: str, month: date) -> list[list[str]] | None:
        """Download and parse one *month's* klines ZIP — see `_fetch_zip_rows`."""
        url = self.BASE_URL.format(symbol=symbol, interval=interval, month=month.strftime("%Y-%m"))
        return self._fetch_zip_rows(url, f"{symbol} {interval} {month.strftime('%Y-%m')}")

    def _fetch_day(self, symbol: str, interval: str, day: date) -> list[list[str]] | None:
        """Download and parse one *day's* klines ZIP — see `_fetch_zip_rows`.

        Used by the daily phase of `backfill` to cover the in-progress
        current month. A 404 here is the normal "not yet published" case
        for today (and often yesterday too, depending on Binance's
        publish lag) — the caller treats it as a skipped day, not an error.
        """
        url = self.DAILY_BASE_URL.format(
            symbol=symbol, interval=interval, day=day.strftime("%Y-%m-%d")
        )
        return self._fetch_zip_rows(url, f"{symbol} {interval} {day.strftime('%Y-%m-%d')}")

    def _sanity_check(
        self,
        symbol: str,
        interval: str,
        month: date,
        rows: list[list[str]],
    ) -> None:
        """Warn (don't raise) if rows-per-month is far from expected.

        Catches accidental schema/cadence drift early — e.g. if Binance ever
        switched a monthly ZIP to daily aggregation we'd see 30 rows where
        ~2880 were expected. Skipped for intervals where one month yields
        few enough rows that absolute deviation is meaningless.
        """
        per_min = self._INTERVAL_MINUTES.get(interval)
        if per_min is None:
            return  # `1M`: a single row per month, not worth checking
        # Use 30 days as the reference for low/high bounds — months range
        # 28..31 days, which is well within the 0.5×/1.5× tolerance below.
        expected = (30 * 24 * 60) // per_min
        if expected < 50:
            return  # large intervals — small absolute counts, skip the check
        n = len(rows)
        if n < expected * 0.5 or n > expected * 1.5:
            logger.warning(
                "klines row count off for %s %s %s: got %d (expected ~%d)",
                symbol,
                interval,
                month.strftime("%Y-%m"),
                n,
                expected,
            )

    def _sanity_check_day(
        self,
        symbol: str,
        interval: str,
        day: date,
        rows: list[list[str]],
    ) -> None:
        """Per-day analogue of `_sanity_check`.

        Same intent — warn-don't-raise on cadence drift — but the expected
        row count is one day's worth, not one month's. For 15m that's 96
        rows; for 1m it's 1440. Intervals whose per-day expectation is
        too small for a robust absolute-deviation check (e.g. `1d` → 1
        row) skip the check entirely, just like the monthly version.
        """
        per_min = self._INTERVAL_MINUTES.get(interval)
        if per_min is None:
            return
        expected = (24 * 60) // per_min
        if expected < 12:
            return  # ≥2h intervals: too few rows/day for a meaningful bound
        n = len(rows)
        if n < expected * 0.5 or n > expected * 1.5:
            logger.warning(
                "klines row count off for %s %s %s: got %d (expected ~%d)",
                symbol,
                interval,
                day.strftime("%Y-%m-%d"),
                n,
                expected,
            )

    @transaction.atomic
    def _persist_month(self, symbol: str, interval: str, rows: list[list[str]]) -> tuple[int, int]:
        """Upsert one month's rows. Returns (created, updated) counts.

        Reuses `BinanceCandlesController._row_to_defaults` verbatim — the
        archive CSV columns match the live JSON kline array element-for-
        element, and that mapper is already tuned to take strings via
        `Decimal(str(...))`. We only need to coerce the two integer
        timestamp columns (`open_time` ms and `close_time` ms) before
        handing the row off, since the mapper passes `row[6]` straight into
        `datetime.fromtimestamp`.
        """
        created = 0
        updated = 0
        for row in rows:
            # Normalise the two int columns the live mapper assumes are
            # already ints — CSV gives us strings.
            row[0] = int(row[0])
            row[6] = int(row[6])
            open_time = datetime.fromtimestamp(row[0] / 1000, tz=UTC)
            _, was_created = Candle.objects.update_or_create(
                symbol=symbol,
                interval=interval,
                open_time=open_time,
                defaults=BinanceCandlesController._row_to_defaults(row),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated
