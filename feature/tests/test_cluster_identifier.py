"""Tests for `ClusterIdentifierController`'s per-window lookback contract.

Most of these monkey-patch the controller's `_fetch_*` and `_latest_close`
methods so the test body controls exactly what the §5 math sees. That
keeps the assertion surface on the public contract — lookback bounds,
window validation, persistence scoping — without coupling to Postgres
test-DB availability. The single `@pytest.mark.django_db` test exercises
the real `compute_and_persist` DELETE scope against an in-memory test DB.

Companion to `test_clustering.py` (math layer); together they cover
the §5 pipeline from raw OI rows down to persisted segments.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from feature.controllers.cluster_identifier import (
    ClusterIdentifierController,
    ClusterMap,
)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _seed_oi_rows(
    start: datetime,
    hours: int,
    base: Decimal = Decimal("1_000_000"),
) -> list[tuple[datetime, Decimal]]:
    """Synthetic 1h OI series: `hours` rows starting at `start`, each
    hour growing by ~5% of `base` so every pairwise delta is positive
    and roughly equal — well above any percentile threshold the
    rolling_threshold helper might compute."""
    out: list[tuple[datetime, Decimal]] = []
    for i in range(hours):
        ts = start + timedelta(hours=i)
        out.append((ts, base + Decimal(i) * (base / Decimal(20))))
    return out


def _seed_5m_ohlc(
    start: datetime,
    hours: int,
) -> list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]]:
    """288 5m bars per day — generate enough to satisfy the density
    floor for the test's window. Constant OHLC (no sweep candles)
    keeps end_time=None across all emitted segments, simplifying
    assertions."""
    out = []
    bars = hours * 12  # 12 × 5m per hour
    for i in range(bars):
        ts = start + timedelta(minutes=5 * i)
        out.append((ts, Decimal("100"), Decimal("100.5"), Decimal("99.5"), Decimal("100")))
    return out


# ---- lookback validation ---------------------------------------------------


def test_identify_rejects_unsupported_lookback():
    """Only members of `SUPPORTED_LOOKBACKS` are accepted."""
    ctrl = ClusterIdentifierController()
    with pytest.raises(ValueError, match="lookback_hours"):
        ctrl.identify("BTCUSDT", lookback_hours=48)


def test_identify_rejects_zero_lookback():
    """Zero is not in the supported set — same rejection path."""
    ctrl = ClusterIdentifierController()
    with pytest.raises(ValueError, match="lookback_hours"):
        ctrl.identify("BTCUSDT", lookback_hours=0)


def test_identify_accepts_each_supported_lookback(monkeypatch):
    """24, 72, 168 must all pass validation. Use a minimal seed so
    the path returns an empty map (we're not testing zone math here)."""
    ctrl = ClusterIdentifierController()
    # Empty OI → early-return empty map path; exercises validation only.
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_oi_1h",
        lambda self, symbol: [],
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_latest_close",
        lambda self, symbol: Decimal("100"),
    )
    for lb in ClusterIdentifierController.SUPPORTED_LOOKBACKS:
        cmap = ctrl.identify("BTCUSDT", lookback_hours=lb)
        assert isinstance(cmap, ClusterMap)
        assert cmap.zones == []
        assert cmap.segments == []


# ---- threshold-window scope (the lookback knob's only effect) -------------


def test_identify_threshold_window_matches_lookback(monkeypatch):
    """`rolling_threshold` is called with `window_hours == lookback_hours`.

    Post-correction the lookback knob controls ONE thing: the §5.2
    per-anchor percentile-calibration window passed to
    `rolling_threshold`. The scan range stays full-history. This test
    captures that exact contract by patching `rolling_threshold` and
    asserting it sees the right `window_hours` for each supported
    lookback.
    """
    import feature.controllers.cluster_identifier as cidmod

    seen_windows: list[float] = []

    def fake_threshold(positive, *, window_hours, percentile, min_samples=10):
        seen_windows.append(window_hours)
        # Return empty thresholds → no anchors qualify → empty map
        # path. We only need to capture window_hours.
        return {}

    monkeypatch.setattr(cidmod, "rolling_threshold", fake_threshold)
    # Provide enough OI to reach _build_zones (which calls rolling_threshold).
    now = _utc(2026, 5, 17, 12)
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_oi_1h",
        lambda self, symbol: _seed_oi_rows(
            now - timedelta(hours=200),
            hours=200,
        ),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_5m_ohlc",
        lambda self, symbol, *, start: _seed_5m_ohlc(start, hours=200),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_latest_close",
        lambda self, symbol: Decimal("100"),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_funding_for_anchors",
        lambda self, symbol, anchors: {},
    )

    ctrl = ClusterIdentifierController()
    for lb in ClusterIdentifierController.SUPPORTED_LOOKBACKS:
        seen_windows.clear()
        ctrl.identify("BTCUSDT", lookback_hours=lb, now=now)
        assert seen_windows == [float(lb)], (
            f"lookback_hours={lb}: expected rolling_threshold called with "
            f"window_hours={lb}.0, saw {seen_windows!r}"
        )


def test_identify_5m_fetch_starts_at_earliest_oi(monkeypatch):
    """`_fetch_5m_ohlc` is called with `start = oi_rows[0][0]`.

    The 5m tape spans the same horizon as the OI scan — earliest OI
    timestamp onward — so historical zones get the full window of
    subsequent 5m candles in which a sweep could occur. The lookback
    knob does NOT clamp this range.
    """
    now = _utc(2026, 5, 17, 12)
    earliest_oi = now - timedelta(hours=240)  # 10 days ago

    def fake_oi(self, symbol):
        # Dense OI from `earliest_oi` to ~now.
        return _seed_oi_rows(earliest_oi, hours=240)

    seen_5m_start: dict[str, datetime] = {}

    def fake_5m(self, symbol, *, start):
        seen_5m_start["v"] = start
        return _seed_5m_ohlc(earliest_oi, hours=240)

    monkeypatch.setattr(ClusterIdentifierController, "_fetch_oi_1h", fake_oi)
    monkeypatch.setattr(ClusterIdentifierController, "_fetch_5m_ohlc", fake_5m)
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_latest_close",
        lambda self, symbol: Decimal("100"),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_funding_for_anchors",
        lambda self, symbol, anchors: {},
    )

    ctrl = ClusterIdentifierController()
    ctrl.identify("BTCUSDT", lookback_hours=24, now=now)
    # The 5m fetch lower bound MUST be the earliest OI ts, not
    # `now - 24h`. A bug here would silently re-clamp the scan.
    assert seen_5m_start["v"] == earliest_oi


def test_identify_scans_full_history(monkeypatch):
    """With OI spanning 240h, a 24h-window run still emits zones
    older than `now − 24h`.

    Proves the scan range is NOT clamped to the lookback window. The
    rolling-percentile threshold for older anchors may differ from
    the recent baseline, but as long as their ΔOI beat their own
    trailing 24h baseline they qualify.
    """
    now = _utc(2026, 5, 17, 12)
    earliest_oi = now - timedelta(hours=240)

    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_oi_1h",
        lambda self, symbol: _seed_oi_rows(earliest_oi, hours=240),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_5m_ohlc",
        lambda self, symbol, *, start: _seed_5m_ohlc(start, hours=240),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_latest_close",
        lambda self, symbol: Decimal("100"),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_funding_for_anchors",
        lambda self, symbol, anchors: {},
    )

    ctrl = ClusterIdentifierController()
    cmap = ctrl.identify("BTCUSDT", lookback_hours=24, now=now)
    # _seed_oi_rows produces uniformly-growing OI, so every pairwise
    # delta is roughly equal — the 90th percentile lands at ~delta
    # and roughly half of the bars qualify (linear-interp tie).
    # Whatever the exact count, we need at least one zone older than
    # `now - 24h` to prove history-scan, AND we need at least one
    # to exist at all to prove the path didn't short-circuit.
    assert cmap.zones, "expected at least one qualifying zone"
    cutoff = now - timedelta(hours=24)
    historical = [z for z in cmap.zones if z.open_time < cutoff]
    assert historical, (
        f"expected at least one zone older than {cutoff.isoformat()}, "
        f"got {[z.open_time.isoformat() for z in cmap.zones]}"
    )


def test_identify_now_parameter_overrides_wall_clock(monkeypatch):
    """`now=<fixed datetime>` lets a backtest harness compute as-of T.

    The returned `ClusterMap.generated_at` must equal the passed `now`,
    NOT `_now()`. Verifies the backtest entry point.
    """
    fixed_now = _utc(2025, 12, 1, 0)

    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_oi_1h",
        lambda self, symbol: [],
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_latest_close",
        lambda self, symbol: Decimal("100"),
    )

    # Patch _now to a sentinel — if `identify` ignores the passed `now`
    # and falls back to `_now()`, generated_at would mismatch.
    sentinel = _utc(2099, 1, 1, 0)
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_now",
        staticmethod(lambda: sentinel),
    )

    ctrl = ClusterIdentifierController()
    cmap = ctrl.identify("BTCUSDT", lookback_hours=168, now=fixed_now)
    assert cmap.generated_at == fixed_now


def test_identify_now_default_uses_wall_clock(monkeypatch):
    """When `now=None` (the production path), the controller resolves
    via `_now()`. Symmetric guard against accidentally hard-wiring
    `now`."""
    sentinel = _utc(2030, 6, 15, 9)
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_now",
        staticmethod(lambda: sentinel),
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_fetch_oi_1h",
        lambda self, symbol: [],
    )
    monkeypatch.setattr(
        ClusterIdentifierController,
        "_latest_close",
        lambda self, symbol: Decimal("100"),
    )
    ctrl = ClusterIdentifierController()
    cmap = ctrl.identify("BTCUSDT", lookback_hours=24)
    assert cmap.generated_at == sentinel


# ---- compute_and_persist DELETE scope (DB-backed) --------------------------


@pytest.mark.django_db
def test_compute_and_persist_delete_scopes_to_lookback(monkeypatch):
    """DELETE is narrowed to `(symbol, lookback_hours)`; other windows survive.

    Seeds the DB with one segment for each of the three supported
    windows, then re-runs `compute_and_persist` for lb=24 with an
    empty result (mocked identify). Only the lb=24 row should be
    gone; the lb=72 and lb=168 rows must remain.
    """
    from data.models import ClusterSegment as ClusterSegmentRow

    t = _utc(2026, 1, 1, 0)
    # Pre-seed one row per window. The values don't matter — only the
    # `(symbol, lookback_hours)` scope does.
    for lb in (24, 72, 168):
        ClusterSegmentRow.objects.create(
            symbol="BTCUSDT",
            side="long_liq",
            price_low=Decimal("100"),
            price_high=Decimal("100.5"),
            price=Decimal("100.25"),
            start_time=t,
            end_time=None,
            source_open_time=t,
            strength=1.0,
            notional=Decimal("1000"),
            long_bias=0.5,
            lookback_hours=lb,
        )
    assert ClusterSegmentRow.objects.count() == 3

    # Mock identify so the recompute emits no segments — the DELETE
    # for lb=24 should still fire.
    monkeypatch.setattr(
        ClusterIdentifierController,
        "identify",
        lambda self, symbol, lookback_hours, *, now=None: ClusterMap(
            symbol=symbol,
            generated_at=_utc(2026, 5, 17, 0),
            anchor_price=Decimal("100"),
            zones=[],
            segments=[],
        ),
    )

    ctrl = ClusterIdentifierController()
    summary = ctrl.compute_and_persist("BTCUSDT", lookback_hours=24)

    assert summary["lookback_hours"] == 24
    assert summary["deleted"] == 1  # only the lb=24 row
    assert summary["created"] == 0

    remaining = list(
        ClusterSegmentRow.objects.filter(symbol="BTCUSDT")
        .order_by("lookback_hours")
        .values_list("lookback_hours", flat=True)
    )
    assert remaining == [72, 168], f"expected lb=72 and lb=168 to survive, got {remaining}"
