"""Controller for fetching Binance USDT-M futures open-interest history.

All concepts for the OI pipeline live here — endpoint URL, parameter
validation, HTTP call, JSON row parsing, Decimal/datetime conversion, and the
idempotent upsert into `OpenInterest`. The matching management command in
`data/management/commands/fetch_binance_open_interest.py` is a logic-free
wrapper that only translates argv into a controller call.

The Binance `openInterestHist` endpoint is public — no API key, no signature,
no auth header is needed. Note the path differs from klines: it lives under
`/futures/data/`, not `/fapi/v1/`. See:
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics

The public endpoint serves only the most recent ~30 days of history (see
`docs/liquidation_framework_concept.md` Section 11.1). Backfill beyond that
window requires a paid source such as Coinalyze.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import requests
from django.db import transaction

from data.models import OpenInterest, Symbol


@dataclass
class FetchResult:
    """Summary returned by `BinanceOpenInterestController.fetch_and_store`."""

    symbol: str
    period: str
    requested: int
    received: int
    created: int
    updated: int


class BinanceOpenInterestController:
    """One-shot fetcher for Binance USDT-M futures OI history (public endpoint)."""

    OPEN_INTEREST_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"
    REQUEST_TIMEOUT = 10  # seconds
    MIN_LIMIT = 1
    MAX_LIMIT = 500  # Binance hard cap for the openInterestHist endpoint
    ALLOWED_SYMBOLS = frozenset(Symbol.values)
    # Authoritative list of `period` values the OI endpoint accepts. This is a
    # strict subset of `Interval.values` — the kline-only intervals (1m, 3m,
    # 8h, 3d, 1w, 1M) are NOT valid here, so we cannot reuse Interval.values.
    # See the endpoint docs linked at the top of this module.
    ALLOWED_PERIODS = frozenset({"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"})

    # ---- public entry point -------------------------------------------------
    def fetch_and_store(
        self,
        symbol: str = "BTCUSDT",
        period: str = "5m",
        limit: int = 500,
    ) -> FetchResult:
        """Fetch the most recent `limit` OI buckets and upsert them.

        Idempotent: re-running with the same args updates the still-open last
        bucket in place (via the unique constraint on
        symbol+period+timestamp) and inserts any newly closed buckets.
        """
        symbol, period, limit = self._validate(symbol, period, limit)
        rows = self._fetch(symbol, period, limit)
        created, updated = self._persist(symbol, period, rows)
        return FetchResult(
            symbol=symbol,
            period=period,
            requested=limit,
            received=len(rows),
            created=created,
            updated=updated,
        )

    # ---- internals ----------------------------------------------------------
    def _validate(self, symbol: str, period: str, limit: int) -> tuple[str, str, int]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if period not in self.ALLOWED_PERIODS:
            raise ValueError(f"period must be one of {sorted(self.ALLOWED_PERIODS)}")
        if not (self.MIN_LIMIT <= limit <= self.MAX_LIMIT):
            raise ValueError(f"limit must be between {self.MIN_LIMIT} and {self.MAX_LIMIT}")
        return normalized_symbol, period, limit

    def _fetch(self, symbol: str, period: str, limit: int) -> list[dict]:
        resp = requests.get(
            self.OPEN_INTEREST_HIST_URL,
            params={"symbol": symbol, "period": period, "limit": limit},
            timeout=self.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _row_to_defaults(row: dict) -> dict:
        """Map a single Binance OI history object to OpenInterest defaults.

        Binance openInterestHist row schema (all values arrive as strings,
        except `timestamp` which is a millisecond epoch integer):
          symbol               redundant; set on the model from the request
          sumOpenInterest      total OI in base coin units (e.g. BTC)
          sumOpenInterestValue total OI in quote-asset notional (USD)
          timestamp            bucket time, ms epoch
        """
        return {
            # Build Decimal from the raw string Binance returns — going via
            # float would silently lose digits.
            "sum_open_interest": Decimal(str(row["sumOpenInterest"])),
            "sum_open_interest_value": Decimal(str(row["sumOpenInterestValue"])),
        }

    @transaction.atomic
    def _persist(self, symbol: str, period: str, rows: list[dict]) -> tuple[int, int]:
        created = 0
        updated = 0
        for row in rows:
            timestamp = datetime.fromtimestamp(row["timestamp"] / 1000, tz=UTC)
            _, was_created = OpenInterest.objects.update_or_create(
                symbol=symbol,
                period=period,
                timestamp=timestamp,
                defaults=self._row_to_defaults(row),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated
