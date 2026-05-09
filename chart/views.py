"""Views for the `chart` app.

One HTML page (`home`) and two thin JSON APIs that share the controller from
the `data` app. The APIs are deliberately small — all fetch/validate/upsert
logic lives in `data.controllers.binance_candles_controller`, all
serialization in `chart.serializers`.
"""

from collections.abc import Callable
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from chart.serializers import candles_payload
from data.controllers import binance_candles_controller
from data.models import Candle, Interval, Symbol

_DEFAULT_LIMIT = 500


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
        qs = Candle.objects.filter(symbol=symbol, interval=interval).order_by(
            "open_time"
        )
        return candles_payload(symbol, interval, qs, fetched=fetched)

    return _run(_do)


@require_POST
def refresh_api(request: HttpRequest, symbol: str, interval: str) -> JsonResponse:
    """Force a fresh fetch + upsert, then return the latest candles."""

    def _do() -> dict:
        result = binance_candles_controller.fetch_and_store(
            symbol=symbol, interval=interval, limit=_DEFAULT_LIMIT
        )
        qs = Candle.objects.filter(symbol=symbol, interval=interval).order_by(
            "open_time"
        )
        payload = candles_payload(symbol, interval, qs, fetched=True)
        payload["result"] = {
            "requested": result.requested,
            "received": result.received,
            "created": result.created,
            "updated": result.updated,
        }
        return payload

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
        return JsonResponse(
            {"error": "server", "message": "Internal error"}, status=500
        )
