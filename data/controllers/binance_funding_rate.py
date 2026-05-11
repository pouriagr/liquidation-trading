"""Controller for fetching Binance USDT-M futures funding-rate history.

All concepts for the funding pipeline live here — endpoint URL, parameter
validation, HTTP call, JSON row parsing, Decimal/datetime conversion, and
the idempotent upsert into `FundingRate`. The matching management command
in `data/management/commands/fetch_binance_funding_rate.py` is a
logic-free wrapper that only translates argv into a controller call.

The Binance `fundingRate` endpoint is public — no API key, no signature,
no auth header is needed. See:
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History

Unlike the OI `openInterestHist` endpoint (which is capped at ~30 days of
history), `fundingRate` serves the **full history** of every settlement
since the symbol was listed when called with `startTime`/`endTime`. That
is why this module owns both "latest N" and "date range" modes — we never
need a separate archive source.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import requests
from django.db import transaction

from data.models import FundingRate, Symbol

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Summary returned by `BinanceFundingRateController.fetch_and_store`."""

    symbol: str
    requested: int
    received: int
    created: int
    updated: int


class BinanceFundingRateController:
    """One-shot fetcher for Binance USDT-M futures funding-rate history."""

    FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
    REQUEST_TIMEOUT = 10  # seconds
    MIN_LIMIT = 1
    MAX_LIMIT = 1000  # Binance hard cap for the fundingRate endpoint
    ALLOWED_SYMBOLS = frozenset(Symbol.values)

    # ---- public entry point -------------------------------------------------
    def fetch_and_store(
        self,
        symbol: str = "BTCUSDT",
        limit: int = 1000,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> FetchResult:
        """Fetch funding-rate settlements and upsert them.

        Two modes share one method:
          - **Range mode**: when `start_time` and/or `end_time` is given,
            page forward in `limit`-row batches by advancing the cursor
            to (last fundingTime + 1ms) until a page returns < `limit`
            rows or the next cursor passes `end_time`.
          - **Latest mode**: with neither given, a single request returns
            the most recent `limit` settlements.

        Idempotent: re-running over an overlapping range updates existing
        rows via the unique constraint on (symbol, funding_time) and
        inserts new ones.
        """
        symbol, limit, start_time, end_time = self._validate(symbol, limit, start_time, end_time)
        if start_time is None and end_time is None:
            logger.info("funding fetch start: symbol=%s mode=latest limit=%d", symbol, limit)
            rows = self._fetch_page(symbol=symbol, limit=limit)
        else:
            logger.info(
                "funding fetch start: symbol=%s mode=range limit=%d start=%s end=%s",
                symbol,
                limit,
                start_time.isoformat() if start_time else "(open)",
                end_time.isoformat() if end_time else "(open)",
            )
            rows = self._paginate(
                symbol=symbol,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )
        created, updated = self._persist(symbol, rows)
        logger.info(
            "funding fetch done: symbol=%s received=%d created=%d updated=%d",
            symbol,
            len(rows),
            created,
            updated,
        )
        return FetchResult(
            symbol=symbol,
            requested=limit,
            received=len(rows),
            created=created,
            updated=updated,
        )

    # ---- internals ----------------------------------------------------------
    def _validate(
        self,
        symbol: str,
        limit: int,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> tuple[str, int, datetime | None, datetime | None]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if not (self.MIN_LIMIT <= limit <= self.MAX_LIMIT):
            raise ValueError(f"limit must be between {self.MIN_LIMIT} and {self.MAX_LIMIT}")
        for label, value in (("start_time", start_time), ("end_time", end_time)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{label} must be a timezone-aware datetime")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start_time must be <= end_time")
        return normalized_symbol, limit, start_time, end_time

    @staticmethod
    def _to_ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def _fetch_page(
        self,
        symbol: str,
        limit: int,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict]:
        # Build params lazily so optional keys are simply absent when
        # unused — Binance treats absent and empty differently on some
        # endpoints, so don't pass empty strings.
        params: dict[str, int | str] = {"symbol": symbol, "limit": limit}
        if start_time is not None:
            params["startTime"] = self._to_ms(start_time)
        if end_time is not None:
            params["endTime"] = self._to_ms(end_time)
        resp = requests.get(
            self.FUNDING_RATE_URL,
            params=params,
            timeout=self.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _paginate(
        self,
        symbol: str,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: int,
    ) -> list[dict]:
        """Drive the range loop, accumulating rows across pages.

        The cursor is the lower bound: each subsequent page starts at
        (last fundingTime + 1ms) so we never re-fetch the boundary row.
        We stop on a short page (no more data in the window) or when the
        next cursor would pass `end_time`.
        """
        all_rows: list[dict] = []
        cursor = start_time
        # Safety cap to avoid pathological infinite loops if Binance ever
        # returns the same boundary row indefinitely. 200 pages × 1000
        # rows = 200k settlements, far beyond any realistic backfill.
        MAX_PAGES = 200
        for page_idx in range(1, MAX_PAGES + 1):
            page = self._fetch_page(
                symbol=symbol,
                limit=limit,
                start_time=cursor,
                end_time=end_time,
            )
            if not page:
                logger.info("funding paginate page=%d: empty page, stopping", page_idx)
                break
            all_rows.extend(page)
            logger.info(
                "funding paginate page=%d: received=%d cursor=%s",
                page_idx,
                len(page),
                cursor.isoformat() if cursor else "(latest)",
            )
            if len(page) < limit:
                break
            last_ms = int(page[-1]["fundingTime"])
            next_cursor = datetime.fromtimestamp(last_ms / 1000, tz=UTC) + timedelta(milliseconds=1)
            if end_time is not None and next_cursor > end_time:
                break
            cursor = next_cursor
        return all_rows

    @staticmethod
    def _row_to_defaults(row: dict) -> dict:
        """Map a single Binance fundingRate object to FundingRate defaults.

        Binance fundingRate row schema (all values arrive as strings,
        except `fundingTime` which is a millisecond epoch integer):
          symbol       redundant; set on the model from the request
          fundingTime  settlement time, ms epoch
          fundingRate  decimal funding rate (signed)
          markPrice    mark price at settlement; may be empty string on
                       older rows — coerced to None to keep ingestion
                       lossless.
        """
        raw_mark = row.get("markPrice")
        # Build Decimal from the raw string Binance returns — going via
        # float would silently lose digits. Coerce missing/empty values
        # to None (Binance returns "" on some older rows that predate the
        # markPrice field); leave any non-empty value, including "0", as
        # Binance reported it rather than second-guessing the API.
        mark_price = Decimal(str(raw_mark)) if raw_mark not in (None, "") else None
        return {
            "funding_rate": Decimal(str(row["fundingRate"])),
            "mark_price": mark_price,
        }

    @transaction.atomic
    def _persist(self, symbol: str, rows: list[dict]) -> tuple[int, int]:
        created = 0
        updated = 0
        for row in rows:
            funding_time = datetime.fromtimestamp(row["fundingTime"] / 1000, tz=UTC)
            _, was_created = FundingRate.objects.update_or_create(
                symbol=symbol,
                funding_time=funding_time,
                defaults=self._row_to_defaults(row),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated
