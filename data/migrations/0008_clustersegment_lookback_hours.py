# Generated for the multi-window rolling-lookback clustering change.
#
# Drops all legacy ClusterSegment rows before adding `lookback_hours`
# as NOT NULL. The legacy rows were computed against the *entire* 1h OI
# history with a fixed 7-day threshold window — semantically different
# from the new "self-contained 24h / 72h / 168h window" output, so
# tagging them with any specific `lookback_hours` would mis-label them
# and pollute the §12.3 confluence sums for one refresh cycle. The
# first refresh after deploy repopulates all three windows.

from django.db import migrations, models


def _drop_legacy_rows(apps, _schema_editor):
    """Delete all ClusterSegment rows.

    Reason for a full wipe rather than a back-fill: legacy rows used
    `THRESHOLD_WINDOW_DAYS=7` over the whole OI history, not a bounded
    24/72/168 h window. Mis-labelling them as `lookback_hours=168`
    would briefly distort the new GET aggregation (rows from the old
    math would sum with rows from the new math). The wipe is cheap —
    rows are recomputed on the next refresh of each symbol.
    """
    ClusterSegment = apps.get_model("data", "ClusterSegment")
    ClusterSegment.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0007_clustersegment"),
    ]

    operations = [
        migrations.RunPython(
            code=_drop_legacy_rows,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveIndex(
            model_name="clustersegment",
            name="cluster_segment_lookup_idx",
        ),
        migrations.AddField(
            model_name="clustersegment",
            name="lookback_hours",
            field=models.PositiveSmallIntegerField(),
        ),
        migrations.AddIndex(
            model_name="clustersegment",
            index=models.Index(
                fields=["symbol", "lookback_hours", "-start_time"],
                name="cluster_segment_lookup_idx",
            ),
        ),
    ]
