from django.apps import AppConfig


class FeatureConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "feature"

    def ready(self) -> None:
        # Import the signals module for its side effects — registers the
        # `pre_save` handler on `data.Candle` that populates `delta`.
        # Imported here (not at module top) so AppConfig.ready() can run
        # after the app registry is fully populated.
        from feature import signals  # noqa: F401
