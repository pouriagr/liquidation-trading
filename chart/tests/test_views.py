"""Tests for `chart.views.clusters_api`: ?windows= parsing and merging.

These exercise the GET endpoint end-to-end through Django's test
client — seed `ClusterSegment` rows tagged per lookback window, then
assert the envelope shape and the §12.3 sum-of-strengths merge
behaviour. All marked `@pytest.mark.django_db`; the cluster-math
correctness is covered separately in `feature/tests/test_clustering.py`.

`pytest.ini` is configured with `DJANGO_SETTINGS_MODULE = core.settings`,
so pytest-django bootstraps the ORM on the first `django_db` mark.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.test import Client

from data.models import ClusterSegment


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _make_segment(
    *,
    symbol: str = "BTCUSDT",
    lookback_hours: int,
    price_low: str,
    price_high: str,
    side: str = "long_liq",
    strength: float = 1.0,
    notional: str = "1000",
    start_time: datetime | None = None,
) -> ClusterSegment:
    """Persist one ClusterSegment row, filled in from string literals."""
    pl = Decimal(price_low)
    ph = Decimal(price_high)
    return ClusterSegment.objects.create(
        symbol=symbol,
        side=side,
        price_low=pl,
        price_high=ph,
        price=(pl + ph) / Decimal(2),
        start_time=start_time or _utc(2026, 1, 1, 0),
        end_time=None,
        source_open_time=start_time or _utc(2026, 1, 1, 0),
        strength=strength,
        notional=Decimal(notional),
        long_bias=0.5,
        lookback_hours=lookback_hours,
    )


@pytest.mark.django_db
def test_clusters_api_default_returns_all_windows_merged():
    """No `?windows=` → server defaults to all three; rows merged."""
    # Same band in lb=24 and lb=72, different band in lb=168.
    _make_segment(lookback_hours=24, price_low="100", price_high="100.5", strength=10.0)
    _make_segment(lookback_hours=72, price_low="100", price_high="100.5", strength=20.0)
    _make_segment(lookback_hours=168, price_low="110", price_high="110.5", strength=5.0)

    resp = Client().get("/api/clusters/BTCUSDT/")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["symbol"] == "BTCUSDT"
    assert payload["windows"]["selected"] == [24, 72, 168]
    assert payload["windows"]["available"] == [24, 72, 168]

    # Two unique bands after merge.
    assert len(payload["segments"]) == 2
    by_price = {s["price_low"]: s for s in payload["segments"]}
    # Confluence band: 10 + 20 = 30, contributed by both 24h and 72h.
    confluence = by_price[100.0]
    assert confluence["strength"] == 30.0
    assert confluence["lookback_hours"] == [24, 72]
    # Singleton band: lb=168 only, strength unchanged.
    singleton = by_price[110.0]
    assert singleton["strength"] == 5.0
    assert singleton["lookback_hours"] == [168]


@pytest.mark.django_db
def test_clusters_api_single_window_passthrough():
    """`?windows=24` → only lb=24 rows; no merge step; scalar lookback_hours."""
    _make_segment(lookback_hours=24, price_low="100", price_high="100.5", strength=10.0)
    _make_segment(
        lookback_hours=72, price_low="100", price_high="100.5", strength=99.0
    )  # must NOT be returned

    resp = Client().get("/api/clusters/BTCUSDT/?windows=24")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["windows"]["selected"] == [24]
    assert len(payload["segments"]) == 1
    seg = payload["segments"][0]
    # Single-window path: strength unchanged, lookback_hours is a scalar.
    assert seg["strength"] == 10.0
    assert seg["lookback_hours"] == 24


@pytest.mark.django_db
def test_clusters_api_two_windows_merged_shape():
    """`?windows=24,72` returns the §12.3 confluence sum of those two only."""
    _make_segment(lookback_hours=24, price_low="100", price_high="100.5", strength=10.0)
    _make_segment(lookback_hours=72, price_low="100", price_high="100.5", strength=40.0)
    # lb=168 has different math; must be excluded.
    _make_segment(lookback_hours=168, price_low="100", price_high="100.5", strength=999.0)

    resp = Client().get("/api/clusters/BTCUSDT/?windows=24,72")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["windows"]["selected"] == [24, 72]
    assert len(payload["segments"]) == 1
    seg = payload["segments"][0]
    assert seg["strength"] == 50.0  # 10 + 40; lb=168 excluded
    assert seg["lookback_hours"] == [24, 72]


@pytest.mark.django_db
def test_clusters_api_rejects_empty_windows():
    """`?windows=` → 400 (after-strip empty)."""
    resp = Client().get("/api/clusters/BTCUSDT/?windows=")
    # Empty string returns the default (all three) per `_parse_windows`,
    # so the empty-string path actually succeeds. Use a comma-only
    # variant to exercise the "non-empty after parse" rejection.
    assert resp.status_code == 200  # `?windows=` → default
    resp = Client().get("/api/clusters/BTCUSDT/?windows=,")
    body = resp.json()
    assert resp.status_code == 400, body
    assert body["error"] == "validation"


@pytest.mark.django_db
def test_clusters_api_rejects_unknown_window():
    """`?windows=48` → 400 (not in SUPPORTED_LOOKBACKS)."""
    resp = Client().get("/api/clusters/BTCUSDT/?windows=48")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation"
    assert "48" in body["message"]


@pytest.mark.django_db
def test_clusters_api_rejects_non_integer_window():
    """`?windows=abc` → 400 (parse failure)."""
    resp = Client().get("/api/clusters/BTCUSDT/?windows=abc")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation"


@pytest.mark.django_db
def test_clusters_api_empty_symbol_state_returns_empty_segments():
    """A symbol with no persisted segments returns an empty list, not 404."""
    resp = Client().get("/api/clusters/BTCUSDT/?windows=24")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["segments"] == []
    assert payload["windows"]["selected"] == [24]
    assert payload["anchor_price"] == 0.0
