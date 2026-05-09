"""Django admin registrations for the data app."""

from django.contrib import admin

from data.models import Candle


@admin.register(Candle)
class CandleAdmin(admin.ModelAdmin):
    list_display = ("symbol", "interval", "open_time", "close", "volume")
    list_filter = ("symbol", "interval")
    search_fields = ("symbol",)
    date_hierarchy = "open_time"
    ordering = ("-open_time",)
