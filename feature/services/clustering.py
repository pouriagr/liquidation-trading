"""Pure-Python math for §5 cluster identification.

The framework (`docs/liquidation_framework_concept.md` §5) describes the
analytical pipeline that turns the foundational data into a liquidation
map:

  1. Positive ΔOI per hour  → accumulation candidates                (§5.2)
  2. Adaptive percentile threshold → significant accumulation hours  (§5.2)
  3. Price-direction (with funding tie-break) → long/short bias      (§5.3)
  4. Leverage PMF + band centroid → projected liquidation prices     (§5.4)
  5. Notional × leverage_score → cluster strength (§5.5 recency       (§5.5)
     applied downstream by the chart, per-bin, anchored at each
     segment's own `source_time`)

This module owns the math; `feature.controllers.cluster_identifier` owns
the I/O. The split mirrors `feature.services.delta` / `feature.services.oi`:
formulas here, ORM there. Nothing in this file imports Django, so the
helpers can be exercised from a plain pytest without bootstrapping the
ORM — and a future bulk-recompute path can reuse the same code over
arrays loaded from elsewhere.

The chart consumer wants **time-bounded** clusters (one rectangle per
"this band was alive from T₀ until a candle swept it or until now"), so
the top-level entry point is `assemble_segments`, not the original
heatmap aggregator. Each `ClusterSegment` carries its own start/end so
the frontend can render a Coinglass-style band rather than the
infinite-horizontal `createPriceLine` the prior heatmap produced.

The sweep-clip behaviour is an *extension* on top of the framework
doc's recency decay: §5.6 says clusters fade with age, not with price
sweeps, but operators reading the chart expect a level to disappear
once price has wicked through it. Both gates apply — each bin of the
segment is recency-weighted by the chart (decay anchored at the
segment's own `source_time`, so bins farther into the band's lifetime
contribute less), and the segment is *additionally* clipped in time
once a candle wick crosses the band.

All monetary inputs are `Decimal` to match the storage precision of
`OpenInterest.sum_open_interest_value` and `Candle.{open,high,low,close}`.
Decimals stay Decimal through projection; only the final strength score
collapses to float because (a) it's a relative score, not a price or
notional, and (b) the chart consumer wants a float anyway.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

# Bound once at module load so the tight loops in `compute_positive_deltas`
# don't reconstruct a `timedelta(hours=1)` per iteration.
_ONE_HOUR = timedelta(hours=1)

# Canonical leverage PMF for retail-dominated USDT-perp pairs — the §5.4
# table, with each banded range *split into its endpoints* rather than
# collapsed to a midpoint. Treating 5× and 10× as separate tiers (instead
# of one "7×") produces two distinct liquidation projections per zone
# instead of one in between — closer to how positions actually liquidate,
# since real traders pick round-number leverages (5, 10, 20, 25, 50, 100)
# not the arithmetic mean.
#
# Shares within each banded range are split equally — the doc gives a
# range like "20× to 25×: 20–30%" without saying which endpoint is more
# common, so 50/50 is the least-presumptive prior. The total share for
# each banded range matches the midpoint of the doc's percentage range:
#
#   5× to 10×   →  5×: 17.5%, 10×: 17.5%   (total 35%, mid of 30–40%)
#   20× to 25×  → 20×: 12.5%, 25×: 12.5%   (total 25%, mid of 20–30%)
#   50×         → 50×: 20%                  (single value, mid of 15–25%)
#   100×        → 100×: 10%                 (single value, mid of 5–15%)
#
# Shares sum to 0.90; the remaining 0.10 represents lower-leverage /
# cross-margin positions whose liquidation prices are too distant to
# project usefully (a 3× long liquidates ~33% below entry — far outside
# any cluster heatmap the decision-maker actually trades around). §5.4
# also says this distribution "should be calibrated against actual
# observed liquidation events"; that calibration is deferred until the
# liquidation feed is wired (see plan §"Out of Scope").
LEVERAGE_PMF: dict[int, float] = {
    5: 0.175,
    10: 0.175,
    20: 0.125,
    25: 0.125,
    50: 0.20,
    100: 0.10,
}


# ---- dataclasses -----------------------------------------------------------
# Carried across the services↔controller boundary. The controller passes
# DB-loaded rows in as raw inputs (`compute_positive_deltas` etc.), then
# packages the results as these dataclasses for both the public API and
# the chart serializer.


@dataclass
class AccumulationZone:
    """One hour where OI grew significantly, plus inferred direction bias.

    Bias is signed: positive = long-heavy (price rose and/or funding +ve),
    negative = short-heavy. |bias| in [0, 1] is a confidence weight — a
    weak rise produces a weak bias, a strong rise produces a strong one,
    per §5.3's "strength of the directional bias should reflect the
    magnitude of the supporting signal".
    """

    open_time: datetime
    price_low: Decimal
    price_high: Decimal
    delta_oi_notional: Decimal  # > 0 by construction (we drop negatives in §5.2)
    long_bias: float  # in [-1, 1]


@dataclass
class Projection:
    """One liquidation projection from one (zone, band, tier, side) tuple.

    `notional` is the *share* of the source zone's growth attributed to
    this projection: zone_notional × band_overlap × leverage_pmf[tier] ×
    side_share. Retained as an intermediate dataclass for backward
    compatibility and testability of `build_projections`; the controller
    no longer consumes it (segments are now the public unit).
    """

    price: Decimal
    side: str  # "long_liq" | "short_liq"
    notional: Decimal
    tier: int
    source_open_time: datetime


@dataclass
class ClusterSegment:
    """One time-bounded liquidation band — the public output of §5.

    Replaces the old `HeatmapBucket`. A segment is the per-(source_zone,
    destination_band, side) liquidation pressure, collapsed over leverage
    tiers (its `strength` sums the leverage-weighted contributions of
    every tier whose projected liquidation price lands in this band). It
    carries explicit time bounds so the chart can draw a rectangle:

      * `start_time` — the source zone's `open_time` (when the
        accumulation happened that produced this projection).
      * `end_time` — the first 5m candle after `start_time` whose
        `[low, high]` overlaps `[price_low, price_high]` (the band was
        swept). `None` means "no overlap found in the input candles",
        which the frontend renders as "draw to the right edge / now".

    `price_low` / `price_high` are the band's anchor-grid edges (a
    geometric bucket of width `band_pct` of the anchor — see
    `bands_covering`); `price` is the band centre, kept for tooltips
    that want a single representative number.

    `notional` (Decimal) and `long_bias` (carried from the source zone)
    are tooltip-only metadata; the chart's fill colour is keyed on
    `strength` alone.
    """

    price_low: Decimal
    price_high: Decimal
    price: Decimal
    side: str  # "long_liq" | "short_liq"
    start_time: datetime
    end_time: datetime | None
    strength: float  # notional · leverage_score(tier) — time-independent
    notional: Decimal
    long_bias: float
    source_open_time: datetime


# ---- §5.2: positive ΔOI and adaptive threshold -----------------------------
def compute_positive_deltas(
    rows: list[tuple[datetime, Decimal]],
) -> list[tuple[datetime, Decimal]]:
    """Pairwise Δ of OI notional; drop non-positive entries.

    `rows` is `(timestamp, sum_open_interest_value)` oldest→newest. The
    output is `(timestamp_of_close, delta)` for every consecutive pair
    whose Δ > 0 — matching §5.2's "only positive OI changes inform
    accumulation analysis. Negative OI changes (positions closing) at the
    same price are a different phenomenon and should not cancel out the
    positive accumulation."

    The timestamp attached to each delta is the *later* of the pair: that's
    the bar during which the growth happened, which is what we'll match
    against `Candle.open_time` for the price-range lookup. A 1h candle
    with `open_time = HH:00` covers `[HH:00, HH+1:00)`, and the 1h OI
    snapshot at `HH+1:00` reports the position-count at the end of that
    same span — so pairing OI[HH+1:00] − OI[HH:00] with candle[HH:00] is
    the correct alignment.
    """
    out: list[tuple[datetime, Decimal]] = []
    for (_t_prev, oi_prev), (t_cur, oi_cur) in zip(rows, rows[1:], strict=False):
        delta = oi_cur - oi_prev
        if delta > 0:
            # Attach to the bar whose price range we'll need next:
            # OI[HH+1:00] − OI[HH:00] grew during the hour starting at HH:00.
            bar_anchor = _hour_floor(t_cur) - _ONE_HOUR
            out.append((bar_anchor, delta))
    return out


def significance_threshold(
    positive_deltas: list[Decimal],
    *,
    percentile: int = 90,
    min_samples: int = 10,
) -> Decimal:
    """The §5.2 adaptive threshold — N-th percentile of positive deltas.

    Returns `Decimal(0)` when fewer than `min_samples` positive deltas
    exist: an under-sampled window gives a degenerate percentile, so the
    safe behaviour is "every positive delta counts" and let the strength
    score downstream sort signal from noise via the recency/leverage
    weights. A normal 7-day window has 168 hours, of which 50–80 are
    typically positive — well above the floor.

    Implemented in pure Python (sort + linear interpolation) so the
    services layer stays Django-free *and* numpy-free; numpy is available
    transitively via pandas but importing it here would couple the math
    module to a heavy dependency for a job that's one sort and two index
    lookups.
    """
    n = len(positive_deltas)
    if n < min_samples:
        return Decimal(0)
    if not 0 <= percentile <= 100:
        raise ValueError("percentile must be in [0, 100]")
    sorted_d = sorted(positive_deltas)
    # Linear interpolation, matches numpy's default `method='linear'` so
    # the behaviour is the one a reader would expect from "the 90th
    # percentile". `rank` is the fractional index into the sorted list.
    rank = (percentile / 100) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_d[lo]
    frac = Decimal(str(rank - lo))
    return sorted_d[lo] + (sorted_d[hi] - sorted_d[lo]) * frac


def rolling_threshold(
    positive_deltas: list[tuple[datetime, Decimal]],
    *,
    window_hours: float = 168.0,
    percentile: int = 90,
    min_samples: int = 10,
) -> dict[datetime, Decimal]:
    """Per-anchor §5.2 adaptive threshold over a sliding time window.

    For each `(t, delta)` in `positive_deltas` returns the percentile
    cutoff computed over the subset of deltas whose timestamps fall in
    `[t − window_hours, t]` (inclusive on both ends). Unlike
    `significance_threshold`, which collapses the full input into a
    single global cutoff, this is the right shape when the analysis
    spans months: a quiet hour from six months ago that *was*
    significant relative to its own surroundings should still register,
    even if compared to today's much larger OI scale it would look
    like noise.

    Anchors with fewer than `min_samples` in-window deltas yield
    `Decimal(0)` — mirrors `significance_threshold`'s degraded-window
    contract so the call site can treat both helpers identically.

    Two-pointer sliding window over the chronologically-sorted input;
    per iteration delegates the percentile math to
    `significance_threshold` on the in-window slice. Complexity is
    O(n · w) where w is the typical in-window count; at a year of 1 h
    OI (n = 8 760, w = 168) that's ~1.5 M comparisons in pure Python —
    well under 100 ms on the hot path.

    Caller responsibility: `positive_deltas` MUST be sorted ascending
    by timestamp. `compute_positive_deltas` already returns the
    correct order, so the typical chain is just
    `rolling_threshold(compute_positive_deltas(rows))`.
    """
    window_td = timedelta(hours=window_hours)
    out: dict[datetime, Decimal] = {}
    lo = 0
    for hi, (t_hi, _) in enumerate(positive_deltas):
        # Slide the left edge forward until the earliest in-window
        # delta's timestamp is within the trailing `window_td`. Both
        # bounds are inclusive — a delta exactly at `t_hi - window_td`
        # is part of the window so the boundary case (`window=24h`,
        # delta exactly 24h old) doesn't silently drop a sample.
        cutoff = t_hi - window_td
        while lo < hi and positive_deltas[lo][0] < cutoff:
            lo += 1
        in_window = [d for _t, d in positive_deltas[lo : hi + 1]]
        out[t_hi] = significance_threshold(
            in_window,
            percentile=percentile,
            min_samples=min_samples,
        )
    return out


# ---- §5.3: long/short composition inference --------------------------------
def direction_bias(
    *,
    price_open: Decimal,
    price_close: Decimal,
    sign_floor_bps: int = 5,
    strong_move_bps: int = 50,
    funding_rate: Decimal | None,
) -> float:
    """Inferred long-bias in [-1, 1] for the accumulation interval.

    Rule of thumb from §5.3:

    * Price rose during accumulation → new longs entered aggressively
      → long-heavy. Magnitude scales with how big the rise was: a
      `strong_move_bps` move (50 bps by default = 0.5%) saturates the
      bias at ±1, smaller moves produce a softer bias.

    * Price was effectively flat (|move| < `sign_floor_bps`, default 5 bps
      = 0.05%) → composition is ambiguous → break the tie with funding:
      positive funding ⇒ long-heavy, negative ⇒ short-heavy. The
      magnitude is held below the price-based regime (caps at ±0.3) to
      reflect that it's a weaker signal than direct price movement.

    Returns 0.0 only when even funding is ambiguous (zero or unknown) —
    callers that need a hard decision should treat `|bias| < ε` as
    "no projection emitted", but the default heatmap path lets a near-
    zero bias just spread the notional 50/50 across long_liq / short_liq.
    """
    if price_open <= 0:
        return 0.0
    move_bps = float((price_close - price_open) / price_open) * 10_000
    if abs(move_bps) >= sign_floor_bps:
        # Clamp to ±strong_move_bps then linearly scale to ±1.
        scaled = max(-1.0, min(1.0, move_bps / strong_move_bps))
        return scaled
    # Tie-break — funding's sign decides, capped weak.
    if funding_rate is None or funding_rate == 0:
        return 0.0
    return 0.3 if funding_rate > 0 else -0.3


# ---- §5.4: leverage projection --------------------------------------------
def bands_covering(
    price_low: Decimal,
    price_high: Decimal,
    *,
    anchor: Decimal,
    band_pct: Decimal,
) -> list[tuple[int, Decimal, float]]:
    """Yield `(index, center, overlap_fraction)` for every band touching the range.

    Bands form a regular geometric grid anchored at `anchor`: band `k`
    spans `[anchor·(1+k·p), anchor·(1+(k+1)·p))` where `p = band_pct`.
    Using a global anchor means two zones at slightly different prices
    still contribute to the *same* band index when they overlap — without
    this, off-by-one quantization would scatter aligned accumulation
    across adjacent bands and dilute the cluster signal.

    `overlap_fraction` is the share of `[price_low, price_high]` that
    falls inside band `k`, so the zone's notional can be allocated by
    multiplying. Sums to 1.0 across the returned list (modulo a final
    Decimal-rounding wisp).

    Edge case: a degenerate `price_low == price_high` zone is treated as
    a single band — whichever band the price falls into gets weight 1.
    """
    if price_high < price_low:
        raise ValueError("price_high must be >= price_low")
    if anchor <= 0 or band_pct <= 0:
        raise ValueError("anchor and band_pct must be positive")

    if price_high == price_low:
        k = _band_index(price_low, anchor=anchor, band_pct=band_pct)
        return [(k, _band_center(k, anchor=anchor, band_pct=band_pct), 1.0)]

    k_lo = _band_index(price_low, anchor=anchor, band_pct=band_pct)
    k_hi = _band_index(price_high, anchor=anchor, band_pct=band_pct)
    span = float(price_high - price_low)

    out: list[tuple[int, Decimal, float]] = []
    for k in range(k_lo, k_hi + 1):
        band_lo = anchor * (Decimal(1) + Decimal(k) * band_pct)
        band_hi = anchor * (Decimal(1) + Decimal(k + 1) * band_pct)
        overlap_lo = max(band_lo, price_low)
        overlap_hi = min(band_hi, price_high)
        overlap = float(overlap_hi - overlap_lo)
        if overlap <= 0:
            continue
        out.append((k, _band_center(k, anchor=anchor, band_pct=band_pct), overlap / span))
    return out


def aggregate_5m_to_1h_candles(
    candles_5m: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
    anchors: list[datetime],
) -> dict[datetime, dict[str, Decimal]]:
    """Bucket 5m OHLC rows into per-anchor 1h aggregates.

    `candles_5m` is `(open_time, open, high, low, close)` oldest→newest.
    `anchors` are the HH:00 UTC timestamps the caller actually needs an
    aggregate for — usually a sparse subset of all possible hours, so
    we only emit those (no point materialising ~8 760 buckets when 875
    are referenced).

    For each anchor with at least one 5m sample in `[anchor, anchor+1h)`
    the result holds `{"open", "high", "low", "close"}` where `open`
    comes from the earliest 5m row in the bucket, `close` from the
    latest, and `high` / `low` are the max / min across all rows. The
    math is identical to what Binance returns for a 1h candle over the
    same trades — Binance's 1h is itself an aggregation over the same
    underlying ticks the 5m sources from.

    Hours with no 5m samples are absent from the output. Caller still
    handles `dict.get(anchor) is None` the same way (skip the zone)
    so a missing-data hour is treated as "ambiguous" rather than
    fabricated.

    Mirrors `feature.services.oi.aggregate_5m_to_1h` (the 1h OI
    derivation) in both shape and philosophy: 1h is a *derived*
    resolution from 5m sources, not a separately-fetched timeframe.
    The framework doc (§12.2 / §12.3) treats 1h as the analysis
    cadence for cluster identification but never asks for 1h candles
    as a raw fetch — `feature/controllers/refresh.py`'s
    `CANDLE_INTERVALS = ("5m", "15m", "4h", "1d")` is the canonical
    doc-aligned bundle, and this helper is what lets the cluster
    analyzer keep operating at 1h cadence without depending on a
    fetched 1h source.

    Pure-Python O(N + A·k) where N is the 5m row count, A is the
    anchor count, and k is the per-bucket row count (≤12 at full
    density). At year-scale N ≈ 105 k and A ≈ 875, well under 100 ms.
    """
    if not candles_5m or not anchors:
        return {}

    # First pass: index 5m rows by their hour anchor. Building once
    # and looking up per anchor is cheaper than bisect+scan when the
    # anchor set is dense (the typical case for cluster identification
    # — most positive-delta hours qualify).
    by_hour: dict[datetime, list[tuple[Decimal, Decimal, Decimal, Decimal]]] = {}
    for open_time, op, hi, lo, cl in candles_5m:
        anchor = _hour_floor(open_time)
        by_hour.setdefault(anchor, []).append((op, hi, lo, cl))

    out: dict[datetime, dict[str, Decimal]] = {}
    for anchor in anchors:
        rows = by_hour.get(anchor)
        if not rows:
            continue
        # `candles_5m` arrives chronologically; appends to `by_hour`
        # therefore preserve order, so rows[0] is the earliest 5m
        # in the bucket and rows[-1] is the latest.
        bucket_high = rows[0][1]
        bucket_low = rows[0][2]
        for _op, hi, lo, _cl in rows[1:]:
            if hi > bucket_high:
                bucket_high = hi
            if lo < bucket_low:
                bucket_low = lo
        out[anchor] = {
            "open": rows[0][0],
            "high": bucket_high,
            "low": bucket_low,
            "close": rows[-1][3],
        }
    return out


def project_liquidation_price(
    band_center: Decimal,
    *,
    side: str,
    leverage: int,
) -> Decimal:
    """Naive liquidation price per §2.1: entry × (1 ∓ 1/L) for long/short.

    Ignores fees, funding accrual, and the maintenance-margin offset that
    moves the actual liquidation a hair closer than 1/L. Per §5.6, all
    cluster estimates are approximate — chasing exact bankruptcy prices
    would imply false precision the inferred-leverage layer above cannot
    support.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    factor = Decimal(1) / Decimal(leverage)
    if side == "long_liq":
        return band_center * (Decimal(1) - factor)
    if side == "short_liq":
        return band_center * (Decimal(1) + factor)
    raise ValueError("side must be 'long_liq' or 'short_liq'")


def build_projections(
    zones: list[AccumulationZone],
    *,
    anchor: Decimal,
    band_pct: Decimal,
    leverage_pmf: dict[int, float] = LEVERAGE_PMF,
) -> list[Projection]:
    """Fan a list of zones out into per-tier, per-side liquidation projections.

    Retained for back-compat and as a testable intermediate; the
    controller's `identify()` no longer calls this — `assemble_segments`
    subsumes both the per-tier fan-out and the per-band aggregation.

    For each zone we split notional first by band overlap (where inside
    the zone's price range did this dollar come from?), then by leverage
    tier (`leverage_pmf` — what fraction of these are 10× vs 50×?), then
    by side: the zone's `long_bias` scores the long/short composition of
    the OI, and we route long-position notional to long_liq projections
    (below the band) and short-position notional to short_liq (above).

    A zero `long_bias` splits 50/50 — that's the §5.3 fall-through when
    both price direction and funding were uninformative.
    """
    out: list[Projection] = []
    for zone in zones:
        ls = _long_share(zone.long_bias)
        ss = 1.0 - ls
        for _k, center, overlap in bands_covering(
            zone.price_low, zone.price_high, anchor=anchor, band_pct=band_pct
        ):
            band_notional = zone.delta_oi_notional * Decimal(str(overlap))
            for tier, pmf_share in leverage_pmf.items():
                tier_notional = band_notional * Decimal(str(pmf_share))
                if ls > 0:
                    out.append(
                        Projection(
                            price=project_liquidation_price(center, side="long_liq", leverage=tier),
                            side="long_liq",
                            notional=tier_notional * Decimal(str(ls)),
                            tier=tier,
                            source_open_time=zone.open_time,
                        )
                    )
                if ss > 0:
                    out.append(
                        Projection(
                            price=project_liquidation_price(
                                center, side="short_liq", leverage=tier
                            ),
                            side="short_liq",
                            notional=tier_notional * Decimal(str(ss)),
                            tier=tier,
                            source_open_time=zone.open_time,
                        )
                    )
    return out


# ---- §5.5: per-segment assembly (time-bounded clusters) --------------------
def leverage_score(tier: int, *, max_tier: int = 100) -> float:
    """log2-scaled weight in (0, 1]: higher leverage → larger reaction.

    `log2(tier) / log2(max_tier)`. So with the default `max_tier=100`,
    the §5.4 tiers come out at ~0.42 (7×), ~0.67 (22×), ~0.85 (50×), and
    1.0 (100×) — encoding §5.5's "leverage concentration" property:
    high-leverage clusters punch above their notional share.
    """
    if tier <= 1 or max_tier <= 1:
        raise ValueError("tier and max_tier must be > 1")
    return math.log2(tier) / math.log2(max_tier)


def recency_weight(age_hours: float, *, halflife_hours: float = 72.0) -> float:
    """Exponential decay; 1.0 at age 0, 0.5 at one half-life, etc.

    The §5.5 "recency" axis: an older accumulation zone is more likely
    to have been closed manually since, and a position closed manually
    no longer has a liquidation. We don't drop old zones — a 7-day-old
    zone still contributes ~0.5²·³³ ≈ 0.13 weight at the default 72h
    half-life — because the §5.6 limitation ("recent vs old cannot be
    distinguished from aggregates") means even a stale zone might still
    be live; we just discount it.
    """
    if age_hours < 0:
        return 1.0  # future-dated input is a clock skew, not a real signal
    return math.exp(-age_hours * math.log(2) / halflife_hours)


def find_consumption_time(
    *,
    price_low: Decimal,
    price_high: Decimal,
    start_time: datetime,
    candles: list[tuple[datetime, Decimal, Decimal]],
) -> datetime | None:
    """First time after `start_time` when a candle wick overlaps the band.

    `candles` is `(open_time, low, high)` oldest→newest. The function
    skips candles dated at or before `start_time` (a zone can't be swept
    by its own accumulation bar or anything earlier), then returns the
    `open_time` of the first candle whose `[low, high]` overlaps the
    `[price_low, price_high]` band. Returns `None` if the band is still
    alive at the end of the input.

    "Overlaps" = `NOT (candle.high < band.price_low OR candle.low >
    band.price_high)`. A degenerate point band (`price_low ==
    price_high`) still works because the strict-less comparisons handle
    the boundary — a candle just kissing the level (high == price_low)
    is treated as a sweep.

    Note: this is an *extension* on top of §5.6's recency decay — the
    framework doc itself doesn't define sweep-based invalidation. The
    rationale is operator intuition: a level price has wicked through
    shouldn't visually persist as if nothing happened. Strength still
    carries the recency weight, so old still-live levels are dimmer
    than fresh ones regardless of sweep status.

    Implementation: `bisect.bisect_right` locates the first index in
    `candles` whose `open_time > start_time`, then we scan forward
    from there. The bisect path costs O(log N) instead of O(prefix)
    — important once history grows to a year (≈ 100 k candles): with
    many zones the cumulative skip work would otherwise be N²/2.
    Tuples compare lexicographically, so a sentinel `Decimal` larger
    than any real price acts as "+∞" in the low/high slots and lets
    the bisect ignore ties on the second/third fields.
    """
    if not candles:
        return None
    sentinel = Decimal("999999999999")
    start_idx = bisect.bisect_right(candles, (start_time, sentinel, sentinel))
    for open_time, low, high in candles[start_idx:]:
        if not (high < price_low or low > price_high):
            return open_time
    return None


def assemble_segments(
    zones: list[AccumulationZone],
    *,
    now: datetime,
    anchor: Decimal,
    band_pct: Decimal,
    consumption_candles: list[tuple[datetime, Decimal, Decimal]],
    leverage_pmf: dict[int, float] = LEVERAGE_PMF,
    max_tier: int = 100,
) -> list[ClusterSegment]:
    """Fan zones into per-(zone, destination-band, side) time-bounded segments.

    The replacement for the old `assemble_heatmap`. Where the heatmap
    collapsed every projection into one anonymous bucket per price band
    (losing the per-zone lifecycle that we need to draw rectangles), this
    keeps one segment per `(zone, dest_band, side)` so the chart can
    paint a rectangle from the zone's `open_time` to the band's sweep
    candle (or to "now" if not yet swept).

    Algorithm — for each zone:

      1. Compute long/short share from `_long_share(zone.long_bias)`.
      2. Fan over source bands covered by the zone's `[price_low,
         price_high]` (existing `bands_covering`). Each source band gets
         a share `overlap` of the zone's total ΔOI notional.
      3. For each `(tier, side)` pair, compute the projected liquidation
         price (`project_liquidation_price`) and the *destination* band
         index it lands in (`_band_index`). Accumulate
         `notional · leverage_score(tier)` into a per-`(dest_band, side)`
         slot — projections from different source bands of the same zone
         that land in the same destination collapse into one segment.
      4. After processing all source bands of the zone, emit one
         `ClusterSegment` per accumulated `(dest_band, side)` slot,
         with `end_time = find_consumption_time(...)` computed over
         `consumption_candles`.

    Note: the server-side strength is **time-independent** —
    `notional · leverage_score`, nothing else. The §5.5 recency axis IS
    applied, but at the chart layer rather than here, and with a
    per-bin anchor at each segment's own `source_time` (the right edge
    of the rolling lookback window that identified the band). See
    `chart/static/chart/js/home.js` (`_paint`): each bin contributes
    `strength · exp(-(bin_time − source_time) · ln2 / halflife)` so a
    band reads as a heat tail fading from bright-at-birth to dim along
    its lifetime. The decay is intentionally viewport-independent —
    pan/zoom doesn't move any cell's colour — which is the property
    that makes the heatmap usable in backtest mode. The earlier
    server-side formulation, `recency_weight(now − zone.open_time)`,
    was wall-clock-anchored: it crushed a $50 M zone from six months
    ago to near-zero strength regardless of how it was being viewed,
    which is why this layer no longer pre-multiplies. `recency_weight`
    and `RECENCY_HALFLIFE_HOURS` here are still the canonical
    definitions of the decay curve; the chart's JS constant
    `RECENCY_HALFLIFE_HOURS` mirrors this one so the framework doc's
    single curve covers both ends. `now` is still a parameter (used
    by the consumption-check timeline and reserved for any future
    server-side decay mode); pass it through anyway.

    Reactivation is a property of the data: two zones at different
    `open_time`s producing into the same dest_band emit two distinct
    `ClusterSegment` instances. The chart renders both rectangles —
    the older one clipped at its sweep candle, the newer one extending
    from its own `open_time` onward. No special-case code needed.

    `consumption_candles` is loaded once by the caller for the whole
    symbol's lookback window and shared across all segments. We pass
    the full list rather than a per-zone slice because slicing in
    Python costs more than the `open_time <= start_time` skip inside
    `find_consumption_time`.

    Returned segments are in zone-major order (zones iterated in the
    caller's order, then per-zone dest_bands in dict-insertion order).
    Callers that need a different order should sort downstream.
    """
    # `now` is intentionally unused under the current strength formula
    # — recency is applied at the chart layer with a per-bin anchor at
    # each segment's `source_time`, not from a single global "now".
    # Kept on the signature so callers don't have to learn a new
    # contract and so a future server-side decay mode can re-introduce
    # the multiplier without touching the public API.
    del now
    out: list[ClusterSegment] = []
    for zone in zones:
        ls = _long_share(zone.long_bias)
        ss = 1.0 - ls

        # acc[(k_dst, side)] -> {"s": float strength, "n": Decimal notional}
        # Accumulating *across source bands and leverage tiers* collapses
        # what would otherwise be many near-identical rectangles into one
        # per destination band per side. Strength is float (the unit-less
        # score the chart consumes); notional stays Decimal so the
        # tooltip can show an exact USD figure.
        acc: dict[tuple[int, str], dict[str, float | Decimal]] = {}

        for _k_src, center, overlap in bands_covering(
            zone.price_low, zone.price_high, anchor=anchor, band_pct=band_pct
        ):
            band_notional = zone.delta_oi_notional * Decimal(str(overlap))
            for tier, pmf_share in leverage_pmf.items():
                tier_notional = band_notional * Decimal(str(pmf_share))
                lscore = leverage_score(tier, max_tier=max_tier)
                for side, side_share in (("long_liq", ls), ("short_liq", ss)):
                    if side_share <= 0:
                        continue
                    side_notional = tier_notional * Decimal(str(side_share))
                    liq_price = project_liquidation_price(center, side=side, leverage=tier)
                    if liq_price <= 0:
                        # A degenerate projection at <=0 (a tiny price hit
                        # by a 100×-stop) makes no physical sense — skip
                        # rather than pollute the heatmap.
                        continue
                    k_dst = _band_index(liq_price, anchor=anchor, band_pct=band_pct)
                    strength_contrib = float(side_notional) * lscore
                    slot = acc.setdefault((k_dst, side), {"s": 0.0, "n": Decimal(0)})
                    slot["s"] = float(slot["s"]) + strength_contrib
                    slot["n"] = Decimal(slot["n"]) + side_notional

        for (k_dst, side), agg in acc.items():
            price_lo = anchor * (Decimal(1) + Decimal(k_dst) * band_pct)
            price_hi = anchor * (Decimal(1) + Decimal(k_dst + 1) * band_pct)
            end_time = find_consumption_time(
                price_low=price_lo,
                price_high=price_hi,
                start_time=zone.open_time,
                candles=consumption_candles,
            )
            out.append(
                ClusterSegment(
                    price_low=price_lo,
                    price_high=price_hi,
                    price=_band_center(k_dst, anchor=anchor, band_pct=band_pct),
                    side=side,
                    start_time=zone.open_time,
                    end_time=end_time,
                    strength=float(agg["s"]),
                    notional=Decimal(agg["n"]),
                    long_bias=zone.long_bias,
                    source_open_time=zone.open_time,
                )
            )
    return out


# ---- §12.3 multi-window confluence -----------------------------------------
def aggregate_segments_across_windows(
    segment_lists: list[list[ClusterSegment]],
) -> list[ClusterSegment]:
    """Sum strengths per `(price_band, side, source_anchor)` across multiple
    window results.

    Each inner list is the §5 output for one lookback window over the
    same symbol (same `_latest_close` anchor, same `PRICE_BAND_PCT`),
    so the `(price_low, price_high)` tuples are guaranteed to align
    exactly across windows. Same band, same accumulation event, in
    multiple windows → strengths sum. Different accumulation events
    at the same band stay distinct — this preserves the §5
    "reactivation" pattern (`assemble_segments` emits one segment per
    zone; a level activated five times over a year is five segments,
    not one).

    The §12.3 multi-resolution confluence boost falls out naturally:
    a (band, anchor, side) triple confirmed by all three windows lands
    at ~3× single-window strength because `assemble_segments` produced
    three contributing segments — identical math, identical
    `source_open_time` — that this helper sums. A (band, anchor, side)
    that qualified in only one window stays at 1× strength.

    Grouping key: `(price_low.quantize(8dp), price_high.quantize(8dp),
    side, source_open_time)`. The 8-dp quantize is defensive against
    the geometric-grid arithmetic (`anchor · (1 + k·pct)`) producing
    a 9th-dp wisp on one path that isn't there on another.
    `source_open_time` is the anchor's `open_time`, identical across
    windows for the same accumulation hour.

    Per-field merge rules (within one group key):
      * `strength`         → sum across windows
      * `notional`         → sum across windows (Decimal-exact)
      * `long_bias`        → notional-weighted average (bias is
        intensive; averaging respects the mix of long/short OI growth
        that fed this band across windows). All three windows on the
        same zone produce identical biases, so this is mathematically
        idempotent in the common case.
      * `start_time`       → identical across windows by construction
        (= `source_open_time`), take the first.
      * `end_time`         → min of the non-None values; None if all
        windows report None. All windows share the same 5m tape, so
        the same sweep candle resolves identically too in the common
        case; the min is defensive for any future divergence.
      * `source_open_time` → identical (it's in the key); take the first.
      * `price`, `price_low`, `price_high` → identical across windows
        by construction; take the first.

    Pure Python, O(total_segments) — a single pass with an accumulator
    dict. The result is sorted by `(start_time, price_low, side)` for
    stable iteration.
    """
    if not segment_lists:
        return []

    # 8-decimal-place quantum so the grouping key collapses any final-step
    # arithmetic wisps from `bands_covering` / `_band_center`.
    eight_dp = Decimal("0.00000001")

    Key = tuple[Decimal, Decimal, str, datetime]
    bucket: dict[Key, dict] = {}

    for segs in segment_lists:
        for s in segs:
            key: Key = (
                s.price_low.quantize(eight_dp),
                s.price_high.quantize(eight_dp),
                s.side,
                s.source_open_time,
            )
            slot = bucket.get(key)
            if slot is None:
                bucket[key] = {
                    "price_low": s.price_low,
                    "price_high": s.price_high,
                    "price": s.price,
                    "side": s.side,
                    "strength": float(s.strength),
                    "notional": Decimal(s.notional),
                    # Carry the bias×notional product so we can divide
                    # by the running notional sum at emit time.
                    "bias_weighted": float(s.long_bias) * float(s.notional),
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "source_open_time": s.source_open_time,
                }
                continue
            slot["strength"] += float(s.strength)
            slot["notional"] += Decimal(s.notional)
            slot["bias_weighted"] += float(s.long_bias) * float(s.notional)
            # `start_time` and `source_open_time` are identical within a
            # group by construction. `end_time` is defensive-min across
            # windows in case the same band's sweep ever resolves
            # differently (today: same 5m tape ⇒ same answer).
            if s.end_time is not None:
                if slot["end_time"] is None or s.end_time < slot["end_time"]:
                    slot["end_time"] = s.end_time

    out: list[ClusterSegment] = []
    for slot in bucket.values():
        notional = slot["notional"]
        long_bias = slot["bias_weighted"] / float(notional) if notional > 0 else 0.0
        out.append(
            ClusterSegment(
                price_low=slot["price_low"],
                price_high=slot["price_high"],
                price=slot["price"],
                side=slot["side"],
                start_time=slot["start_time"],
                end_time=slot["end_time"],
                strength=slot["strength"],
                notional=notional,
                long_bias=long_bias,
                source_open_time=slot["source_open_time"],
            )
        )
    # Stable order — chart and tests both expect ascending start_time.
    out.sort(key=lambda s: (s.start_time, s.price_low, s.side))
    return out


# ---- internals -------------------------------------------------------------
def _band_index(price: Decimal, *, anchor: Decimal, band_pct: Decimal) -> int:
    """Which band does this price fall into, on the geometric anchor grid?"""
    # (price / anchor − 1) / band_pct, floored. Decimal division stays
    # exact until the final cast; we floor toward −∞ so negative offsets
    # (prices below anchor) round down correctly.
    offset = (price / anchor - Decimal(1)) / band_pct
    return int(math.floor(float(offset)))


def _band_center(k: int, *, anchor: Decimal, band_pct: Decimal) -> Decimal:
    """Centroid price of band `k`."""
    return anchor * (Decimal(1) + (Decimal(k) + Decimal("0.5")) * band_pct)


def _long_share(bias: float) -> float:
    """Long-share of the OI given a signed bias in [-1, 1].

    bias=+1 → 1.0 (pure longs), bias=0 → 0.5 (ambiguous), bias=−1 → 0.0
    (pure shorts). Clamped at both ends in case a caller passes a value
    slightly outside the nominal range.
    """
    return max(0.0, min(1.0, (1.0 + bias) / 2.0))


def _hour_floor(ts: datetime) -> datetime:
    """Floor a tz-aware datetime to its containing whole hour."""
    return ts.replace(minute=0, second=0, microsecond=0)
