"""Signal handlers that wire `feature` formulas onto `data` writes.

The `feature` app owns the per-bar delta formula (see
`feature.services.delta`). To keep the dependency direction one-way
(`feature → data`), `data` must not import that formula itself; instead
we register a `pre_save` handler on `data.Candle` here, so the value of
`Candle.delta` is populated *before* the row is written, on every save
path (controller upserts, admin edits, raw ORM saves).

Connected from `feature.apps.FeatureConfig.ready()`.
"""

from django.db.models.signals import pre_save
from django.dispatch import receiver

from data.models import Candle
from feature.services.delta import compute_delta


@receiver(pre_save, sender=Candle)
def set_candle_delta(sender, instance: Candle, **kwargs) -> None:
    """Populate `Candle.delta` before every save.

    Fires for `Candle.objects.create / save / update_or_create` — which
    covers both Binance controller write paths (live fetch and archive
    backfill, see `data/controllers/binance_candles.py` and
    `data/controllers/binance_klines_archive.py`).

    Skipped when the inputs are missing — e.g. a partial save where
    only some fields are set — so we never replace a previously-good
    Δ with a `NULL`.

    `bulk_create` bypasses signals. We don't use it for `Candle` today;
    if that changes, the bulk payload must populate `delta` itself.
    """
    if instance.taker_buy_base_volume is None or instance.volume is None:
        return
    instance.delta = compute_delta(instance.taker_buy_base_volume, instance.volume)
