"""Tests for the §5 cluster math layer.

The module under test (`feature/services/clustering.py`) is intentionally
Django-free, so these tests run under bare `pytest` without bootstrapping
the ORM — no `@pytest.mark.django_db`, no DB connection, no fixtures.
That's the cheapest possible safety net for the most algorithm-heavy
file in the codebase: a regression in `find_consumption_time` or
`assemble_segments` would silently mis-render the cluster overlay
without any other observable failure, so the unit coverage is here to
hold the contract.

Coverage targets (see `plans/zesty-wandering-spark.md` §5):
  1. find_consumption_time — basic sweep, no-sweep, before-start
  2. assemble_segments — basic, consumption-clipping, reactivation
  3. (Optional smoke) recency decay survives the refactor
"""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from feature.services.clustering import (
    AccumulationZone,
    ClusterSegment,
    aggregate_5m_to_1h_candles,
    aggregate_segments_across_windows,
    assemble_segments,
    find_consumption_time,
    rolling_threshold,
)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    """Tz-aware UTC datetime constructor — keeps the test data terse."""
    return datetime(year, month, day, hour, tzinfo=UTC)


def _zone(
    open_time: datetime,
    low: str,
    high: str,
    *,
    delta: str = "1000000",
    bias: float = 0.5,
) -> AccumulationZone:
    """Build a zone from string literals to avoid Decimal/float surprises."""
    return AccumulationZone(
        open_time=open_time,
        price_low=Decimal(low),
        price_high=Decimal(high),
        delta_oi_notional=Decimal(delta),
        long_bias=bias,
    )


# ---- find_consumption_time --------------------------------------------------


def test_find_consumption_time_basic_sweep():
    """First candle whose [low, high] overlaps the band sets end_time."""
    # band [99, 101]; t=2 candle wicks 98→100, overlapping at 100.
    candles = [
        (_utc(2026, 1, 1, 0), Decimal("102"), Decimal("105")),  # high above, no overlap
        (_utc(2026, 1, 1, 1), Decimal("101.5"), Decimal("103")),
        (_utc(2026, 1, 1, 2), Decimal("98"), Decimal("100")),  # SWEEP — overlaps [99,101]
        (_utc(2026, 1, 1, 3), Decimal("95"), Decimal("96")),
    ]
    result = find_consumption_time(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_time=_utc(2026, 1, 1, 0),
        candles=candles,
    )
    assert result == _utc(2026, 1, 1, 2)


def test_find_consumption_time_no_sweep_returns_none():
    """No candle reaches the band → end_time is None (still alive)."""
    candles = [
        (_utc(2026, 1, 1, 0), Decimal("102"), Decimal("105")),
        (_utc(2026, 1, 1, 1), Decimal("103"), Decimal("106")),
    ]
    result = find_consumption_time(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_time=_utc(2026, 1, 1, 0),
        candles=candles,
    )
    assert result is None


def test_find_consumption_time_ignores_candles_before_start():
    """A band-touching candle BEFORE start_time must not count.

    The zone "starts" at its source open_time; anything earlier
    couldn't have swept what didn't yet exist.
    """
    candles = [
        # This one overlaps the band but is dated *before* start_time.
        (_utc(2026, 1, 1, 0), Decimal("99.5"), Decimal("100.5")),
        # Later candles all stay clear of the band.
        (_utc(2026, 1, 1, 5), Decimal("102"), Decimal("103")),
    ]
    result = find_consumption_time(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_time=_utc(2026, 1, 1, 1),  # one hour after the overlapping candle
        candles=candles,
    )
    assert result is None


# ---- assemble_segments ------------------------------------------------------


def test_assemble_segments_basic():
    """One pure-long zone, no consumption candles → only long_liq, open-ended."""
    z = _zone(_utc(2026, 1, 1, 0), "100", "100", bias=1.0)  # bias=1 → pure longs
    segments = assemble_segments(
        [z],
        now=_utc(2026, 1, 7, 0),
        anchor=Decimal("100"),
        band_pct=Decimal("0.001"),
        consumption_candles=[],
    )
    assert segments, "expected at least one segment per leverage tier"
    # bias=1.0 means short_share = 0, so no short_liq segments are emitted.
    assert all(s.side == "long_liq" for s in segments)
    # No candles → nothing can sweep → all segments still alive.
    assert all(s.end_time is None for s in segments)


def test_assemble_segments_consumption_clips_end_time():
    """A candle wicking through a projected band sets that segment's end_time.

    A 50× long on a $100 zone projects to liq_price = 100·(1 − 1/50) = $98.
    The destination band on a 0.10% anchor grid is `[98, 98.1)`. A candle
    wicking 97.5 → 98.5 overlaps that band, so the 50× long_liq segment
    must end at the candle's open_time.
    """
    z = _zone(_utc(2026, 1, 1, 0), "100", "100", bias=1.0)
    candles = [(_utc(2026, 1, 1, 10), Decimal("97.5"), Decimal("98.5"))]
    segments = assemble_segments(
        [z],
        now=_utc(2026, 1, 7, 0),
        anchor=Decimal("100"),
        band_pct=Decimal("0.001"),
        consumption_candles=candles,
    )
    # The destination band [98, 98.1) is the one that should be clipped.
    swept = [
        s for s in segments if s.side == "long_liq" and s.price_low <= Decimal("98") < s.price_high
    ]
    assert swept, "expected a 50× long_liq segment at price 98"
    assert swept[0].end_time == _utc(2026, 1, 1, 10)
    # Segments at other prices (e.g. the 100× projection at 99) should
    # remain alive — the same candle's high (98.5) is below those bands.
    above = [s for s in segments if s.side == "long_liq" and s.price_low >= Decimal("99")]
    assert above, "expected at least one un-swept long_liq segment above 98.5"
    assert all(s.end_time is None for s in above)


def test_reactivation_yields_two_segments():
    """Same band, two zones at different times, with a sweep between them.

    z1 accumulates at t=0, gets swept at t=6h. z2 accumulates at t=12h —
    same price → same destination band → a *new* segment with its own
    start_time and an open `end_time`. The chart consumer reads this as
    "the level reactivated after being swept".
    """
    z1 = _zone(_utc(2026, 1, 1, 0), "100", "100", bias=1.0)
    z2 = _zone(_utc(2026, 1, 1, 12), "100", "100", bias=1.0)
    candles = [(_utc(2026, 1, 1, 6), Decimal("97.5"), Decimal("98.5"))]
    segments = assemble_segments(
        [z1, z2],
        now=_utc(2026, 1, 7, 0),
        anchor=Decimal("100"),
        band_pct=Decimal("0.001"),
        consumption_candles=candles,
    )
    # Pick out the long_liq segments on the [98, 98.1) band specifically.
    on_98 = [
        s for s in segments if s.side == "long_liq" and s.price_low <= Decimal("98") < s.price_high
    ]
    assert len(on_98) == 2, f"expected exactly two segments at price 98, got {len(on_98)}"

    by_start = {s.start_time: s for s in on_98}
    z1_seg = by_start[_utc(2026, 1, 1, 0)]
    z2_seg = by_start[_utc(2026, 1, 1, 12)]
    # First segment: swept by the t=6h candle.
    assert z1_seg.end_time == _utc(2026, 1, 1, 6)
    # Second segment: the t=6h candle is *before* its start_time and is
    # therefore skipped — no later candles exist, so it stays alive.
    assert z2_seg.end_time is None


def test_strength_independent_of_age():
    """Two identical zones at different ages produce identical strengths.

    Recency-as-strength was dropped on purpose: it dimmed historical
    bands so aggressively (a six-month-old zone decayed to ~10⁻¹⁹
    of its intrinsic value) that the chart became useless for
    backtest viewing — old levels were always faint regardless of
    how large they were at the time. Strength is now
    `notional · leverage_score(tier)` and carries no time-of-call
    dependency. Visual position on the time axis is the only carrier
    of age; sweep-clipping handles invalidation.

    Regression guard: if a future refactor reintroduces a `recency`
    factor without making it an opt-in client-side multiplier, this
    test catches it.
    """
    now = _utc(2026, 1, 4, 0)
    fresh = _zone(now, "100", "100", bias=1.0)  # age 0 h
    aged = _zone(_utc(2026, 1, 1, 0), "100", "100", bias=1.0)  # age 72 h (one half-life)
    ancient = _zone(_utc(2025, 7, 4, 0), "100", "100", bias=1.0)  # age 6 months

    common = dict(
        now=now,
        anchor=Decimal("100"),
        band_pct=Decimal("0.001"),
        consumption_candles=[],
    )
    fresh_segs = assemble_segments([fresh], **common)
    aged_segs = assemble_segments([aged], **common)
    ancient_segs = assemble_segments([ancient], **common)

    fresh_map = {(s.price_low, s.side): s.strength for s in fresh_segs}
    aged_map = {(s.price_low, s.side): s.strength for s in aged_segs}
    ancient_map = {(s.price_low, s.side): s.strength for s in ancient_segs}

    assert fresh_map.keys() == aged_map.keys() == ancient_map.keys()
    for key, fresh_strength in fresh_map.items():
        # Strength must be identical across all three ages, byte-equal.
        # No float tolerance needed — we're not multiplying by
        # anything time-dependent anymore.
        assert aged_map[key] == fresh_strength, f"aged drift at {key}"
        assert ancient_map[key] == fresh_strength, f"ancient drift at {key}"


# ---- rolling_threshold -----------------------------------------------------


def test_rolling_threshold_basic_per_anchor():
    """Each anchor's threshold is the percentile of deltas in its trailing window.

    Three deltas at t=0/1h/2h with values 10/100/1000; window=72h,
    percentile=50, min_samples=1. Each anchor sees an expanding
    window because all earlier samples are still in-window:
      * anchor 0 sees [10]            → median 10
      * anchor 1 sees [10, 100]       → 50th-percentile midpoint = 55
      * anchor 2 sees [10, 100, 1000] → median 100
    """
    deltas = [
        (_utc(2026, 1, 1, 0), Decimal("10")),
        (_utc(2026, 1, 1, 1), Decimal("100")),
        (_utc(2026, 1, 1, 2), Decimal("1000")),
    ]
    result = rolling_threshold(
        deltas,
        window_hours=72.0,
        percentile=50,
        min_samples=1,
    )
    assert result[_utc(2026, 1, 1, 0)] == Decimal("10")
    # Linear-interp percentile: rank = 0.5·(2−1) = 0.5 → halfway between 10 and 100 = 55.
    assert result[_utc(2026, 1, 1, 1)] == Decimal("55.0")
    assert result[_utc(2026, 1, 1, 2)] == Decimal("100")


def test_rolling_threshold_window_slides_out_old_samples():
    """Anchors outside the trailing window must not contribute to the threshold.

    Eight deltas 1h apart, window=3h, percentile=50, min_samples=1.
    The last anchor's in-window set is the trailing 4 (inclusive on
    both ends: t-3h, t-2h, t-1h, t), NOT the first four. We feed
    increasing values so a "didn't slide" bug would push the median
    much lower than the correct one.
    """
    base = _utc(2026, 1, 1, 0)
    deltas = [(base + timedelta(hours=i), Decimal(10 * (i + 1))) for i in range(8)]
    # i=0..7 → values 10, 20, ..., 80
    result = rolling_threshold(
        deltas,
        window_hours=3.0,
        percentile=50,
        min_samples=1,
    )
    # Last anchor at i=7 (value 80) sees in-window {50, 60, 70, 80}
    # → linear-interp 50th percentile = halfway between 60 and 70 = 65.
    last = base + timedelta(hours=7)
    assert result[last] == Decimal(
        "65.0"
    ), f"expected median 65 over trailing 4 samples, got {result[last]!r}"
    # Sanity: the first anchor (t=0) sees only [10] → 10. Confirms
    # the window doesn't accidentally pull in future samples either.
    assert result[base] == Decimal("10")


def test_rolling_threshold_below_min_samples_returns_zero():
    """Anchor with fewer than `min_samples` in-window deltas → Decimal(0).

    Mirrors `significance_threshold`'s degraded-window contract: a
    too-thin window can't produce a meaningful percentile, so the
    safe answer is "every positive delta counts" and let the
    strength score downstream do the sorting.
    """
    base = _utc(2026, 1, 1, 0)
    deltas = [
        (base, Decimal("10")),
        (base + timedelta(hours=1), Decimal("20")),
        (base + timedelta(hours=2), Decimal("30")),
    ]
    # min_samples=5 — none of the anchors can ever satisfy it with
    # only 3 input deltas, so every threshold should be 0.
    result = rolling_threshold(
        deltas,
        window_hours=24.0,
        percentile=90,
        min_samples=5,
    )
    assert all(v == Decimal(0) for v in result.values())


# ---- find_consumption_time — bisect-optimised prefix skip ------------------


def test_find_consumption_time_handles_long_prefix_fast():
    """Bisect skip keeps year-scale calls fast even on the last-candle case.

    10_000 candles, sweep at the very last one with start_time just
    before it. A linear-prefix-skip implementation would walk all
    9_999 candles before the match; bisect cuts that to a single
    log-N jump. Wall-clock guard catches regressions if the
    optimisation ever gets dropped.
    """
    base = _utc(2026, 1, 1, 0)
    candles = [
        (base + timedelta(minutes=5 * i), Decimal("100"), Decimal("100.5")) for i in range(10_000)
    ]
    # Replace the last one with a candle that overlaps the [99, 101] band.
    sweep_time = base + timedelta(minutes=5 * 9_999)
    candles[-1] = (sweep_time, Decimal("98"), Decimal("100"))
    start = base + timedelta(minutes=5 * 9_998)

    t0 = time.perf_counter()
    result = find_consumption_time(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_time=start,
        candles=candles,
    )
    elapsed = time.perf_counter() - t0

    assert result == sweep_time
    # 50 ms is generous — the bisect path should finish in
    # microseconds; the slack absorbs CI noise.
    assert elapsed < 0.05, f"find_consumption_time too slow: {elapsed:.3f}s"


def test_find_consumption_time_bisect_boundary_cases():
    """`start_time == candle.open_time` excludes that candle; between-candles starts work.

    The original implementation used `open_time <= start_time` to
    skip; the bisect rewrite must preserve the same boundary
    semantic (a candle dated exactly at `start_time` cannot sweep
    a zone that begins at the same instant).
    """
    candles = [
        (_utc(2026, 1, 1, 0), Decimal("99.5"), Decimal("100.5")),  # would sweep
        (_utc(2026, 1, 1, 1), Decimal("99.5"), Decimal("100.5")),  # would sweep
    ]
    # Case 1: start_time equals the first candle's open_time. The
    # first candle is excluded, so the sweep is detected at t=1.
    result = find_consumption_time(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_time=_utc(2026, 1, 1, 0),
        candles=candles,
    )
    assert result == _utc(2026, 1, 1, 1)

    # Case 2: start_time falls strictly between two candles —
    # bisect should land at the index after the earlier one. The
    # candle dated *after* start_time is the first overlap.
    result = find_consumption_time(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_time=_utc(2026, 1, 1, 0) + timedelta(minutes=30),
        candles=candles,
    )
    assert result == _utc(2026, 1, 1, 1)


# ---- aggregate_5m_to_1h_candles --------------------------------------------


def test_aggregate_5m_to_1h_candles_basic():
    """12 consecutive 5m candles in one hour aggregate into one OHLC bucket.

    open ← first 5m's open, close ← last 5m's close, high ← max of
    highs, low ← min of lows. Identical to what Binance returns for
    the same 1h slot — both sides aggregate over the same trades.
    """
    anchor = _utc(2026, 1, 1, 0)
    # 12 5m candles at 00:00, 00:05, ..., 00:55.
    candles = []
    for i in range(12):
        ot = anchor + timedelta(minutes=5 * i)
        # Vary open/close/high/low so each candidate (open, close, high, low)
        # has a uniquely-identifiable winner per the aggregation rule.
        op = Decimal(100 + i)
        cl = Decimal(100 + i) + Decimal("0.5")
        hi = Decimal(105 + i)
        lo = Decimal(95 + i)
        candles.append((ot, op, hi, lo, cl))

    out = aggregate_5m_to_1h_candles(candles, [anchor])
    assert anchor in out
    bucket = out[anchor]
    # open = first candle (i=0) open = 100; close = last candle (i=11) close = 111.5
    assert bucket["open"] == Decimal("100")
    assert bucket["close"] == Decimal("111.5")
    # high = max across all 12 = i=11 → 105 + 11 = 116
    assert bucket["high"] == Decimal("116")
    # low = min across all 12 = i=0 → 95
    assert bucket["low"] == Decimal("95")


def test_aggregate_5m_to_1h_candles_partial_hour():
    """A bucket with only some 5m slots still aggregates over what's there.

    Mirrors the framework's "missing data is a quiet skip" rule —
    we don't fabricate values across gaps. The few candles present
    contribute their open / close / high / low exactly as if they
    were the complete set.
    """
    anchor = _utc(2026, 1, 1, 0)
    # Only the 3rd, 7th, and 11th 5m slots (00:10, 00:30, 00:50).
    candles = [
        (
            anchor + timedelta(minutes=10),
            Decimal("100"),
            Decimal("103"),
            Decimal("99"),
            Decimal("101"),
        ),
        (
            anchor + timedelta(minutes=30),
            Decimal("101"),
            Decimal("105"),
            Decimal("97"),
            Decimal("102"),
        ),
        (
            anchor + timedelta(minutes=50),
            Decimal("102"),
            Decimal("104"),
            Decimal("100"),
            Decimal("103.5"),
        ),
    ]
    out = aggregate_5m_to_1h_candles(candles, [anchor])
    assert anchor in out
    bucket = out[anchor]
    # open = open of the earliest present row (00:10).
    assert bucket["open"] == Decimal("100")
    # close = close of the latest present row (00:50).
    assert bucket["close"] == Decimal("103.5")
    # high = max(103, 105, 104) = 105.
    assert bucket["high"] == Decimal("105")
    # low = min(99, 97, 100) = 97.
    assert bucket["low"] == Decimal("97")


def test_aggregate_5m_to_1h_candles_skips_unanchored_hours():
    """Hours not in the anchor list are absent from the output.

    Critical for cluster identification: the caller only asks for
    aggregates at the hours that qualified as accumulation candidates
    (~10% of all hours). The aggregator should not materialise the
    rest — that's a 10× pointless cost on year-scale runs.
    """
    base = _utc(2026, 1, 1, 0)
    # 24 hours of 5m candles (288 rows total).
    candles = [
        (base + timedelta(minutes=5 * i), Decimal(100), Decimal(101), Decimal(99), Decimal(100))
        for i in range(288)
    ]
    # Only ask for two anchors: hour 0 and hour 12.
    anchors = [base, base + timedelta(hours=12)]
    out = aggregate_5m_to_1h_candles(candles, anchors)
    assert set(out.keys()) == set(anchors)
    # And both buckets are well-formed.
    for anchor in anchors:
        assert set(out[anchor].keys()) == {"open", "high", "low", "close"}


# ---- aggregate_segments_across_windows -------------------------------------


def _seg(
    *,
    price_low: str,
    price_high: str,
    side: str,
    start_time: datetime,
    end_time: datetime | None = None,
    strength: float = 1.0,
    notional: str = "1000",
    long_bias: float = 0.5,
    source_open_time: datetime | None = None,
) -> ClusterSegment:
    """ClusterSegment factory from string literals.

    `source_open_time` defaults to `start_time` (matches the production
    path where `assemble_segments` sets them equal). `price` is the
    band centre, computed here as the midpoint so the test data stays
    self-consistent.
    """
    pl = Decimal(price_low)
    ph = Decimal(price_high)
    return ClusterSegment(
        price_low=pl,
        price_high=ph,
        price=(pl + ph) / Decimal(2),
        side=side,
        start_time=start_time,
        end_time=end_time,
        strength=strength,
        notional=Decimal(notional),
        long_bias=long_bias,
        source_open_time=source_open_time or start_time,
    )


def test_aggregate_two_windows_shared_band_sums_strengths():
    """§12.3 confluence: same band in two windows → summed strength."""
    t = _utc(2026, 1, 1, 0)
    w1 = [
        _seg(
            price_low="100",
            price_high="100.5",
            side="long_liq",
            start_time=t,
            strength=100.0,
            notional="2000",
        )
    ]
    w2 = [
        _seg(
            price_low="100",
            price_high="100.5",
            side="long_liq",
            start_time=t,
            strength=40.0,
            notional="500",
        )
    ]
    merged = aggregate_segments_across_windows([w1, w2])
    assert len(merged) == 1
    s = merged[0]
    assert s.strength == 140.0
    assert s.notional == Decimal("2500")


def test_aggregate_three_windows_band_in_all_three_sums_three_strengths():
    """A band confirmed by all three windows lands at the sum of all three."""
    t = _utc(2026, 1, 1, 0)
    w1 = [
        _seg(
            price_low="50000",
            price_high="50250",
            side="short_liq",
            start_time=t,
            strength=100.0,
            notional="1000",
        )
    ]
    w2 = [
        _seg(
            price_low="50000",
            price_high="50250",
            side="short_liq",
            start_time=t,
            strength=80.0,
            notional="800",
        )
    ]
    w3 = [
        _seg(
            price_low="50000",
            price_high="50250",
            side="short_liq",
            start_time=t,
            strength=60.0,
            notional="600",
        )
    ]
    merged = aggregate_segments_across_windows([w1, w2, w3])
    assert len(merged) == 1
    assert merged[0].strength == 240.0
    assert merged[0].notional == Decimal("2400")


def test_aggregate_singleton_band_passes_through_unchanged():
    """A band present in only one window survives byte-equal."""
    t = _utc(2026, 1, 1, 0)
    original = _seg(
        price_low="100",
        price_high="100.5",
        side="long_liq",
        start_time=t,
        strength=42.5,
        notional="1234",
        long_bias=0.25,
    )
    merged = aggregate_segments_across_windows([[original], [], []])
    assert len(merged) == 1
    out = merged[0]
    assert out.price_low == original.price_low
    assert out.price_high == original.price_high
    assert out.side == original.side
    assert out.strength == original.strength
    assert out.notional == original.notional
    assert out.long_bias == original.long_bias
    assert out.start_time == original.start_time
    assert out.end_time is None


def test_aggregate_long_bias_notional_weighted_average():
    """Bias is intensive; merge via notional-weighted average.

    (+0.5 · 200 + −0.1 · 50) / 250 = (100 + −5) / 250 = 0.38.
    """
    t = _utc(2026, 1, 1, 0)
    w1 = [
        _seg(
            price_low="100",
            price_high="100.5",
            side="long_liq",
            start_time=t,
            long_bias=0.5,
            notional="200",
        )
    ]
    w2 = [
        _seg(
            price_low="100",
            price_high="100.5",
            side="long_liq",
            start_time=t,
            long_bias=-0.1,
            notional="50",
        )
    ]
    merged = aggregate_segments_across_windows([w1, w2])
    assert len(merged) == 1
    assert abs(merged[0].long_bias - 0.38) < 1e-9


def test_aggregate_distinct_source_anchors_stay_separate():
    """Same band, different `source_open_time` → two separate segments.

    Critical for the §5 reactivation pattern: `assemble_segments`
    emits one segment per zone, so a band activated five times over a
    year is five segments. The merge must NOT collapse those into one
    rectangle anchored at the earliest appearance — that would hide
    recent reactivations under historical buildups.

    Confluence is per-anchor: if two windows both qualified anchor
    T₁ at the same band, they sum at T₁; if they qualified different
    anchors, both anchors survive with their own strengths.
    """
    early = _utc(2026, 1, 1, 0)
    late = _utc(2026, 1, 5, 0)
    # Two windows, each producing a segment at the *same* band but
    # different anchors. Strengths must NOT merge — these are distinct
    # accumulation events at the same price level.
    w1 = [
        _seg(price_low="100", price_high="100.5", side="long_liq", start_time=late, strength=10.0)
    ]
    w2 = [
        _seg(price_low="100", price_high="100.5", side="long_liq", start_time=early, strength=20.0)
    ]
    merged = aggregate_segments_across_windows([w1, w2])
    assert len(merged) == 2
    by_anchor = {s.source_open_time: s for s in merged}
    assert by_anchor[early].strength == 20.0
    assert by_anchor[late].strength == 10.0


def test_aggregate_same_anchor_two_windows_sums():
    """Same band AND same source_open_time across two windows → 1 segment.

    The §12.3 confluence boost applies per accumulation event. Two
    windows that both qualified the same hour at the same band
    contribute their strengths additively — this is the doc's
    prescribed boost and the central reason for the multi-window UI.
    """
    t = _utc(2026, 1, 1, 0)
    w1 = [_seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, strength=10.0)]
    w2 = [_seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, strength=20.0)]
    merged = aggregate_segments_across_windows([w1, w2])
    assert len(merged) == 1
    assert merged[0].strength == 30.0
    assert merged[0].source_open_time == t


def test_aggregate_end_time_is_earliest_non_null():
    """The earliest observed sweep wins; all-None stays None.

    All windows share the same 5m candle tape — when one detects a
    sweep, it's the true sweep. A later window's `None` reflects only
    that its own time-bound search didn't reach the sweep candle, not
    that the band is still alive.
    """
    t = _utc(2026, 1, 1, 0)
    sweep_a = _utc(2026, 1, 2, 0)
    sweep_b = _utc(2026, 1, 3, 0)

    # Case 1: one window swept, two didn't → earliest sweep wins.
    w1 = [_seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, end_time=None)]
    w2 = [
        _seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, end_time=sweep_b)
    ]
    w3 = [
        _seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, end_time=sweep_a)
    ]
    merged = aggregate_segments_across_windows([w1, w2, w3])
    assert merged[0].end_time == sweep_a  # min of {sweep_a, sweep_b}

    # Case 2: all None → stays None.
    w1 = [_seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, end_time=None)]
    w2 = [_seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, end_time=None)]
    merged_alive = aggregate_segments_across_windows([w1, w2])
    assert merged_alive[0].end_time is None


def test_aggregate_groups_distinct_bands_separately():
    """Different `(price_low, price_high, side)` keys → separate rows.

    A 24h-window's $100 long band and a 168h-window's $110 long band
    are different levels — the merge must NOT collapse them.
    """
    t = _utc(2026, 1, 1, 0)
    w1 = [_seg(price_low="100", price_high="100.5", side="long_liq", start_time=t, strength=10.0)]
    w2 = [_seg(price_low="110", price_high="110.5", side="long_liq", start_time=t, strength=20.0)]
    # Same band, different side → also distinct.
    w3 = [_seg(price_low="100", price_high="100.5", side="short_liq", start_time=t, strength=30.0)]
    merged = aggregate_segments_across_windows([w1, w2, w3])
    assert len(merged) == 3
    keys = {(s.price_low, s.side) for s in merged}
    assert keys == {
        (Decimal("100"), "long_liq"),
        (Decimal("110"), "long_liq"),
        (Decimal("100"), "short_liq"),
    }


def test_aggregate_empty_input_returns_empty():
    """No windows → empty result. No exceptions."""
    assert aggregate_segments_across_windows([]) == []
    assert aggregate_segments_across_windows([[], [], []]) == []
