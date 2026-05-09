"""Django admin registrations for the data app."""

from django.contrib import admin

from data.models import Candle, OpenInterest


@admin.register(Candle)
class CandleAdmin(admin.ModelAdmin):
    list_display = ("symbol", "interval", "open_time", "close", "volume")
    list_filter = ("symbol", "interval")
    search_fields = ("symbol",)
    date_hierarchy = "open_time"
    ordering = ("-open_time",)


@admin.register(OpenInterest)
class OpenInterestAdmin(admin.ModelAdmin):
    list_display = (
        "symbol",
        "period",
        "timestamp",
        "sum_open_interest_value",
        "sum_open_interest",
    )
    list_filter = ("symbol", "period")
    search_fields = ("symbol",)
    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)
