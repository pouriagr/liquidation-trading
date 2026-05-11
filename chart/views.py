"""Views for the `chart` app.

One HTML page (`home`) and two thin JSON APIs that share the controller from
the `data` app. The APIs are deliberately small — all fetch/validate/upsert
logic lives in `data.controllers.binance_candles_controller`, all
serialization in `chart.serializers`.
"""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from chart.serializers import (
    candles_payload,
    cvd_payload,
    funding_payload,
    oi_payload,
)
from data.controllers import binance_candles_controller
from data.models import Candle, FundingRate, Interval, OpenInterest, Symbol
from feature.controllers import cvd_controller, refresh_controller

_DEFAULT_LIMIT = 500
# CVD lookback in days — matches `feature.controllers.refresh.LOOKBACK_DAYS`
# so the indicator sub-pane spans the same time window as the candle pane
# after a successful refresh. `CVDController.series` is vectorised with
# pandas rolling, so even the largest case (5m → ~105k anchors) stays
# inside a typical HTTP round-trip budget.
_CVD_LOOKBACK_DAYS = 365


def home(request: HttpRequest) -> HttpResponse:
    """Render the full-viewport candlestick chart page."""
    return render(
        request,
        "chart/home.html",
        {
            "symbols": Symbol.choices,
            "intervals": Interval.choices,
            "initial_symbol": Symbol.BTCUSDT.value,
            "initial_interval": Interval.MIN_15.value,
        },
    )


@require_GET
def candles_api(request: HttpRequest, symbol: str, interval: str) -> JsonResponse:
    """Return DB candles for (symbol, interval); auto-fetch if empty.

    Auto-fetch fires only when the DB has zero rows for the pair — explicit
    user-driven topping-up is the Refresh button's job.
    """

    def _do() -> dict:
        fetched = False
        if not Candle.objects.filter(symbol=symbol, interval=interval).exists():
            binance_candles_controller.fetch_and_store(
                symbol=symbol, interval=interval, limit=_DEFAULT_LIMIT
            )
            fetched = True
        qs = Candle.objects.filter(symbol=symbol, interval=interval).order_by("open_time")
        return candles_payload(symbol, interval, qs, fetched=fetched)

    return _run(_do)


@require_POST
def refresh_api(request: HttpRequest, symbol: str, interval: str) -> JsonResponse:
    """Run the full 15m multi-source refresh, then return the 15m candle payload.

    All orchestration — fetch-vs-backfill per source, OI 1h derivation,
    per-source error capture — lives in `RefreshController`. This view is
    deliberately thin: validation is the controller's job (it raises
    `ValueError` on non-15m intervals, which `_run` turns into HTTP 400),
    and the candle payload is assembled from the same `candles_payload`
    serializer the GET endpoint uses.
    """

    def _do() -> dict:
        result = refresh_controller.refresh(symbol=symbol, interval=interval)
        qs = Candle.objects.filter(symbol=symbol, interval=interval).order_by("open_time")
        payload = candles_payload(symbol, interval, qs, fetched=True)
        payload["refresh"] = {
            "decision_interval": result.decision_interval,
            "sources": [asdict(s) for s in result.sources],
        }
        return payload

    return _run(_do)


# ---- indicator APIs --------------------------------------------------------
# Three read-only endpoints feeding the sub-pane under the candle chart.
# Each one is a thin wrapper: ORM read (or controller call) → payload
# helper → JSON envelope via `_run`. The data they expose is what
# `feature.controllers.refresh_controller.refresh()` has already
# ingested/derived — these views never trigger upstream fetches.
@require_GET
def oi_api(request: HttpRequest, symbol: str, period: str) -> JsonResponse:
    """Return all OpenInterest rows for (symbol, period), oldest→newest."""

    def _do() -> dict:
        rows = (
            OpenInterest.objects.filter(symbol=symbol, period=period)
            .order_by("timestamp")
            .values("timestamp", "sum_open_interest_value")
        )
        return oi_payload(symbol, period, rows)

    return _run(_do)


@require_GET
def funding_api(request: HttpRequest, symbol: str) -> JsonResponse:
    """Return all FundingRate settlements for `symbol`, oldest→newest."""

    def _do() -> dict:
        rows = (
            FundingRate.objects.filter(symbol=symbol)
            .order_by("funding_time")
            .values("funding_time", "funding_rate")
        )
        return funding_payload(symbol, rows)

    return _run(_do)


@require_GET
def cvd_api(request: HttpRequest, symbol: str, interval: str) -> JsonResponse:
    """Return ~1 year of CVD anchors for (symbol, interval)."""

    def _do() -> dict:
        series = cvd_controller.series_for_lookback(
            symbol=symbol, interval=interval, days=_CVD_LOOKBACK_DAYS
        )
        return cvd_payload(symbol, interval, series)

    return _run(_do)


# ---- shared error envelope -------------------------------------------------
def _run(fn: Callable[[], dict[str, Any]]) -> JsonResponse:
    """Translate controller exceptions into JSON error responses.

    `ValueError`  -> 400 (bad symbol/interval/limit; raised by the controller)
    `requests.RequestException` -> 502 (Binance unreachable / HTTP error)
    everything else -> 500
    """
    try:
        return JsonResponse(fn())
    except ValueError as e:
        return JsonResponse({"error": "validation", "message": str(e)}, status=400)
    except requests.RequestException:
        return JsonResponse(
            {"error": "upstream", "message": "Binance request failed"},
            status=502,
        )
    except Exception:  # noqa: BLE001 — last-resort envelope
        return JsonResponse({"error": "server", "message": "Internal error"}, status=500)
