"""Controller for fetching Binance USDT-M futures candles.

All concepts for the candles pipeline live here — endpoint URL, parameter
validation, HTTP call, JSON row parsing, Decimal/datetime conversion, and the
idempotent upsert into `Candle`. The matching management command in
`data/management/commands/fetch_binance_candles.py` is a logic-free wrapper
that only translates argv into a controller call.

The Binance kline endpoint is public — no API key, no signature, no auth
header is needed. See:
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import requests
from django.db import transaction

from data.models import Candle, Interval, Symbol


@dataclass
class FetchResult:
    """Summary returned by `BinanceCandlesController.fetch_and_store`."""

    symbol: str
    interval: str
    requested: int
    received: int
    created: int
    updated: int


class BinanceCandlesController:
    """One-shot fetcher for Binance USDT-M futures klines (public endpoint)."""

    KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
    REQUEST_TIMEOUT = 10  # seconds
    MIN_LIMIT = 1
    MAX_LIMIT = 1500  # Binance hard cap for the klines endpoint
    # Source of truth for allowed symbols/intervals is the choices module —
    # don't duplicate the lists here.
    ALLOWED_SYMBOLS = frozenset(Symbol.values)
    ALLOWED_INTERVALS = frozenset(Interval.values)

    # ---- public entry point -------------------------------------------------
    def fetch_and_store(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "15m",
        limit: int = 1500,
    ) -> FetchResult:
        """Fetch the most recent `limit` candles and upsert them.

        Idempotent: re-running with the same args updates the still-open last
        candle in place (via the unique constraint on
        symbol+interval+open_time) and inserts any newly closed candles.
        """
        symbol, interval, limit = self._validate(symbol, interval, limit)
        rows = self._fetch(symbol, interval, limit)
        created, updated = self._persist(symbol, interval, rows)
        return FetchResult(
            symbol=symbol,
            interval=interval,
            requested=limit,
            received=len(rows),
            created=created,
            updated=updated,
        )

    # ---- internals ----------------------------------------------------------
    def _validate(self, symbol: str, interval: str, limit: int) -> tuple[str, str, int]:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in self.ALLOWED_SYMBOLS:
            raise ValueError(f"symbol must be one of {sorted(self.ALLOWED_SYMBOLS)}")
        if interval not in self.ALLOWED_INTERVALS:
            raise ValueError(f"interval must be one of {sorted(self.ALLOWED_INTERVALS)}")
        if not (self.MIN_LIMIT <= limit <= self.MAX_LIMIT):
            raise ValueError(f"limit must be between {self.MIN_LIMIT} and {self.MAX_LIMIT}")
        return normalized_symbol, interval, limit

    def _fetch(self, symbol: str, interval: str, limit: int) -> list[list]:
        resp = requests.get(
            self.KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=self.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _row_to_defaults(row: list) -> dict:
        """Map a single Binance kline array to Candle field defaults.

        Binance kline schema (index -> field):
          0  open time (ms)
          1  open
          2  high
          3  low
          4  close
          5  volume (base asset)
          6  close time (ms)
          7  quote asset volume
          8  number of trades
          9  taker buy base asset volume
          10 taker buy quote asset volume
          11 ignore
        """
        return {
            "close_time": datetime.fromtimestamp(row[6] / 1000, tz=UTC),
            # Build Decimal from the raw string Binance returns — going via
            # float would silently lose digits.
            "open": Decimal(str(row[1])),
            "high": Decimal(str(row[2])),
            "low": Decimal(str(row[3])),
            "close": Decimal(str(row[4])),
            "volume": Decimal(str(row[5])),
            "quote_volume": Decimal(str(row[7])),
            "trades": int(row[8]),
            "taker_buy_base_volume": Decimal(str(row[9])),
            "taker_buy_quote_volume": Decimal(str(row[10])),
        }

    @transaction.atomic
    def _persist(self, symbol: str, interval: str, rows: list[list]) -> tuple[int, int]:
        created = 0
        updated = 0
        for row in rows:
            open_time = datetime.fromtimestamp(row[0] / 1000, tz=UTC)
            _, was_created = Candle.objects.update_or_create(
                symbol=symbol,
                interval=interval,
                open_time=open_time,
                defaults=self._row_to_defaults(row),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated
