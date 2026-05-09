"""Django admin registrations for the data app."""

from django.contrib import admin

from data.models import Candle, FundingRate, OpenInterest


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


@admin.register(FundingRate)
class FundingRateAdmin(admin.ModelAdmin):
    list_display = ("symbol", "funding_time", "funding_rate", "mark_price")
    list_filter = ("symbol",)
    search_fields = ("symbol",)
    date_hierarchy = "funding_time"
    ordering = ("-funding_time",)
