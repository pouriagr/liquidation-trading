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
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import requests
from django.db import transaction
from requests.adapters import HTTPAdapter

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

    # Worker count for the parallel daily-fetch pool. Binance publishes no
    # monthly metrics ZIPs (verified: `monthly/metrics/` is empty on the S3
    # bucket), so a 1-year backfill is fundamentally 365 HTTP requests —
    # connection reuse + a small worker pool is the only lever for wall
    # clock. 8 sits comfortably below urllib3's default 10-per-host
    # connection pool and well under any plausible CDN soft-rate on the
    # public archive. Bounded to [1, 32] to catch fat-fingered env values.
    CONCURRENCY: int = max(1, min(32, int(os.environ.get("BINANCE_ARCHIVE_CONCURRENCY", "8"))))

    # ---- public entry point -------------------------------------------------
    def backfill(self, symbol: str, start: date, end: date) -> BackfillResult:
        """Backfill OI buckets for `symbol` over the inclusive date range.

        Idempotent: re-running with the same args updates rows in place via
        the (symbol, period, timestamp) unique constraint on `OpenInterest`.
        Per-day 404s are logged and counted as skipped, not raised.
        """
        symbol, start, end = self._validate(symbol, start, end)

        days: list[date] = []
        cur = start
        while cur <= end:
            days.append(cur)
            cur += timedelta(days=1)
        total_days = len(days)

        logger.info(
            "metrics backfill start: symbol=%s start=%s end=%s (%d days, concurrency=%d)",
            symbol,
            start.isoformat(),
            end.isoformat(),
            total_days,
            self.CONCURRENCY,
        )

        days_attempted = 0
        days_skipped = 0
        days_succeeded = 0
        rows_received = 0
        rows_created = 0
        rows_updated = 0

        # IO-bound fetches run in parallel under a shared Session so each
        # request reuses the same TLS connection. Persistence stays on the
        # main thread so `_persist_day`'s `@transaction.atomic` keeps a
        # single DB connection and per-day INFO logs come out in order.
        #
        # `requests.Session` is thread-safe for concurrent `.get()` calls
        # — its internal urllib3 pool serialises connection checkout — and
        # we cap pool size to CONCURRENCY explicitly so a future bump of
        # CONCURRENCY > 10 doesn't trigger urllib3's "pool is full,
        # discarding connection" warning.
        t0 = time.monotonic()
        with (
            self._build_session() as session,
            ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool,
        ):
            futures = [pool.submit(self._fetch_day, symbol, day, session=session) for day in days]
            logger.info(
                "metrics backfill: %d daily fetches dispatched (workers=%d)",
                len(futures),
                self.CONCURRENCY,
            )
            try:
                for idx, (day, fut) in enumerate(zip(days, futures, strict=True), start=1):
                    days_attempted += 1
                    rows = fut.result()
                    if rows is None:
                        days_skipped += 1
                        elapsed, eta = self._eta(t0, idx, total_days)
                        logger.info(
                            "metrics backfill day %d/%d: %s SKIP (archive not published) "
                            "t=%.1fs eta=%s",
                            idx,
                            total_days,
                            day.isoformat(),
                            elapsed,
                            eta,
                        )
                    else:
                        self._sanity_check(symbol, day, rows)
                        created, updated = self._persist_day(symbol, rows)
                        days_succeeded += 1
                        rows_received += len(rows)
                        rows_created += created
                        rows_updated += updated
                        elapsed, eta = self._eta(t0, idx, total_days)
                        logger.info(
                            "metrics backfill day %d/%d: %s rows=%d created=%d updated=%d "
                            "t=%.1fs eta=%s",
                            idx,
                            total_days,
                            day.isoformat(),
                            len(rows),
                            created,
                            updated,
                            elapsed,
                            eta,
                        )
            except Exception:
                # Don't keep ~CONCURRENCY in-flight fetches running after a
                # fatal error on one of them. `cancel_futures=True` drops
                # anything that hasn't started yet; in-flight HTTP requests
                # complete naturally and their results are discarded.
                pool.shutdown(wait=False, cancel_futures=True)
                raise

        duration = time.monotonic() - t0
        rate = (days_attempted / duration) if duration > 0 else 0.0
        logger.info(
            "metrics backfill done: symbol=%s days=%d ok/%d skip "
            "rows received=%d created=%d updated=%d duration=%.1fs rate=%.2fd/s",
            symbol,
            days_succeeded,
            days_skipped,
            rows_received,
            rows_created,
            rows_updated,
            duration,
            rate,
        )

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

    def _fetch_day(
        self,
        symbol: str,
        day: date,
        *,
        session: requests.Session | None = None,
    ) -> list[dict] | None:
        """Download and parse one day's metrics ZIP.

        Returns a list of CSV rows as dicts, or None if the file is not
        available (HTTP 404). Any other HTTP/network error is raised.

        Accepts an optional `session` so the parallel `backfill` loop can
        share TCP/TLS connections across workers; called without one (the
        default) it falls back to the module-level `requests.get`, which
        is what an ad-hoc caller from the shell would do.
        """
        url = self.BASE_URL.format(symbol=symbol, date=day.isoformat())
        getter = session.get if session is not None else requests.get
        resp = getter(url, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 404:
            # Caller logs the user-facing SKIP at info; this is just the
            # plumbing-level URL for debugging.
            logger.debug("metrics archive 404: %s %s", symbol, day.isoformat())
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

    @staticmethod
    def _eta(start_monotonic: float, done: int, total: int) -> tuple[float, str]:
        """Return (elapsed_seconds, eta_string) for the per-day progress log.

        The ETA is a simple linear extrapolation over the already-completed
        days; that's good enough for a backfill where per-day cost is roughly
        constant. Formatted as `mm:ss` for readability — a raw "1124s" is
        harder to translate to "should I get coffee" than "18:44".
        """
        elapsed = time.monotonic() - start_monotonic
        if done <= 0 or total <= done:
            return elapsed, "done"
        remaining = (elapsed / done) * (total - done)
        mins, secs = divmod(int(remaining), 60)
        return elapsed, f"{mins:d}:{secs:02d}"

    def _build_session(self) -> requests.Session:
        """Build a `requests.Session` sized for our parallel fetch pool.

        Mounting an explicit `HTTPAdapter` lets us size the urllib3
        connection pool to `CONCURRENCY`. Without this, urllib3 uses its
        default `pool_maxsize=10`, which is fine for the default of 8
        workers but would emit a "Connection pool is full, discarding
        connection" warning the moment someone sets
        `BINANCE_ARCHIVE_CONCURRENCY=16`.
        """
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=self.CONCURRENCY, pool_maxsize=self.CONCURRENCY)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    @transaction.atomic
    def _persist_day(self, symbol: str, rows: list[dict]) -> tuple[int, int]:
        """Bulk-upsert one day's rows. Returns (created, updated).

        Uses Postgres `INSERT … ON CONFLICT (symbol, period, timestamp) DO
        UPDATE` via Django 5.2's `bulk_create(update_conflicts=True, …)`,
        so 288 rows are written in **one** SQL round-trip instead of the
        old 288 × (SELECT + UPDATE) pattern from `update_or_create`. That
        drops per-day persistence from ~1.5s to ~50ms — the same lever
        the framework's archive backfill needs to actually benefit from
        the parallel HTTP fetcher above. Without this change, parallel
        fetches just queue up while the main thread serially chews
        through DB writes.

        We still split the result into `created` vs `updated` so the
        per-day INFO log keeps its existing shape. A single pre-count
        against the unique constraint partitions the payload; that's
        one extra index scan per day, which is negligible next to the
        ~95 % win on the upsert itself.

        `bulk_create` bypasses signals, but `OpenInterest` has none —
        the only `pre_save` handler in this project is the delta-fill
        on `Candle` (see `feature.signals.set_candle_delta`). It also
        skips `auto_now_add` / `auto_now` field defaults, so we set
        `created_at` and `updated_at` explicitly. `created_at` is in
        the INSERT column list but omitted from `update_fields`, so the
        ON CONFLICT branch preserves the original creation timestamp
        and only refreshes `updated_at`.
        """
        now = datetime.now(UTC)
        objs = [
            OpenInterest(
                symbol=symbol,
                period=self.PERIOD,
                timestamp=self._parse_create_time(row["create_time"]),
                created_at=now,
                updated_at=now,
                **self._row_to_defaults(row),
            )
            for row in rows
        ]

        # Pre-count existing rows so the caller can keep reporting
        # `created`/`updated` in the per-day INFO log. The unique index
        # on (symbol, period, timestamp) makes this an index-only scan.
        updated = OpenInterest.objects.filter(
            symbol=symbol,
            period=self.PERIOD,
            timestamp__in=[o.timestamp for o in objs],
        ).count()

        OpenInterest.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["symbol", "period", "timestamp"],
            update_fields=[
                "sum_open_interest",
                "sum_open_interest_value",
                "updated_at",
            ],
        )

        created = len(objs) - updated
        return created, updated
