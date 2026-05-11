"""Aggregate 5-minute Open Interest snapshots into 1-hour buckets.

Open Interest is a *stock*, not a flow: each row reports the standing
position-count at that instant, not what changed during the preceding
interval. The right 1-hour summary of a window of 5-minute snapshots
is therefore "the value as of the end of the hour" — i.e. the *last*
5-minute sample whose timestamp falls inside `[HH:00, HH:00+1h)`.

Summing five-minute snapshots would conflate twelve independent
position-counts into a meaningless number. Averaging would smooth over
the very changes the cluster-identification analysis is trying to
detect (`docs/liquidation_framework_concept.md` §12.2 uses recent
24h–168h lookbacks of 1h OI for exactly that purpose).

The helper is Django-free so the same code can run from a controller
that does the DB I/O, from a test fixture, or from a future bulk
ingestion path — see also `feature.services.delta` for the same
convention.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass
class Hourly:
    """One derived 1-hour OI row, ready to be persisted.

    `timestamp` is always the *anchor* of the hour bucket (HH:00 UTC);
    `sum_open_interest` / `sum_open_interest_value` are the close
    values for that hour — see module docstring for the rationale.
    """

    timestamp: datetime
    sum_open_interest: Decimal
    sum_open_interest_value: Decimal


def _hour_anchor(ts: datetime) -> datetime:
    """Floor a tz-aware datetime to its containing whole hour."""
    return ts.replace(minute=0, second=0, microsecond=0)


def aggregate_5m_to_1h(
    rows_5m: list[tuple[datetime, Decimal, Decimal]],
) -> list[Hourly]:
    """Group `(timestamp, oi, oi_value)` 5-minute rows into 1-hour rows.

    Inputs must be ordered by `timestamp` ascending. For each whole UTC
    hour with at least one 5-minute sample, emits exactly one `Hourly`
    whose `timestamp` is the start of that hour (HH:00) and whose OI
    values are taken from the *latest* 5-minute sample in the bucket.

    Hours with no samples in the input are simply absent from the
    output — we don't fabricate a value across a gap. Callers that
    need gap-detection semantics (e.g. CVD) layer them on top.
    """
    if not rows_5m:
        return []

    out: list[Hourly] = []
    bucket_anchor: datetime | None = None
    bucket_close: tuple[datetime, Decimal, Decimal] | None = None

    def _flush():
        # Materialise the in-progress bucket. Called whenever the input
        # crosses an hour boundary and once at the end of the loop.
        nonlocal bucket_anchor, bucket_close
        if bucket_anchor is not None and bucket_close is not None:
            out.append(
                Hourly(
                    timestamp=bucket_anchor,
                    sum_open_interest=bucket_close[1],
                    sum_open_interest_value=bucket_close[2],
                )
            )
        bucket_anchor = None
        bucket_close = None

    for ts, oi, oi_value in rows_5m:
        anchor = _hour_anchor(ts)
        if anchor != bucket_anchor:
            _flush()
            bucket_anchor = anchor
        # Inputs are sorted, so the *last* row we see for a given anchor
        # is the close of that hour by construction.
        bucket_close = (ts, oi, oi_value)
    _flush()

    return out


def hour_floor(ts: datetime) -> datetime:
    """Return the start (HH:00) of the UTC hour containing `ts`.

    Exposed for callers that need to align a query range with the
    aggregator's bucket boundaries — see
    `feature.controllers.oi_aggregator`.
    """
    return _hour_anchor(ts)


def hour_ceil(ts: datetime) -> datetime:
    """Return the start of the next whole hour strictly after `ts`."""
    return _hour_anchor(ts) + timedelta(hours=1)
