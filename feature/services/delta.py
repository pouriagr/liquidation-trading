"""The per-bar volume-delta formula — the canonical home of Δ.

CVD (Cumulative Volume Delta) is the fourth foundational pillar of the
trading framework (`docs/liquidation_framework_concept.md` §3.4). It is
the running sum of *per-bar* volume delta:

    Δ = aggressive_buy_volume − aggressive_sell_volume
      = taker_buy_base_volume − (volume − taker_buy_base_volume)
      = 2 × taker_buy_base_volume − volume

`Candle.delta` (in the `data` app) stores Δ for every bar. The formula
itself lives here — a tiny, Django-free utility — so that:

  * `feature/signals.py` can call it from a `pre_save` handler on Candle,
  * Tests can import it without bootstrapping the ORM,
  * Any future bulk-loader path can populate Δ from a single source of
    truth instead of inlining the math.

Importing this from `data` is the *only* place where `data` is allowed
to depend on `feature` — and even then it's mediated by a signal
handler, not a direct call from `data`'s code. See AGENTS.md.
"""

from decimal import Decimal


def compute_delta(taker_buy_base_volume: Decimal, volume: Decimal) -> Decimal:
    """Return Δ for one bar. Signed: negative when aggressive sells dominate."""
    return 2 * taker_buy_base_volume - volume
