"""URL routes for the `chart` app.

Mounted at the project root in `core.urls` under namespace `chart`. The
home page (HTML) plus a small set of JSON APIs the home page's JS calls:

  * `chart:candles`  — the candlestick series for the main pane.
  * `chart:refresh`  — POST, runs the multi-source 15m refresh bundle.
  * `chart:oi`       — Open Interest series for the indicator sub-pane.
  * `chart:funding`  — funding-rate series for the indicator sub-pane.
  * `chart:cvd`      — windowed CVD series for the indicator sub-pane.
  * `chart:clusters` — §5 liquidation cluster map (zones + heatmap),
                       overlaid on the candle pane rather than the
                       indicator sub-pane. Takes only `symbol`; the
                       lookback comes through as a `?lookback_days=`
                       query param so the path stays cacheable.

The indicator endpoints are intentionally split by data type so each
URL carries only the parameters that source needs (OI has a period,
funding has no period, CVD has a candle interval); a unified endpoint
would force a least-common-denominator signature.
"""

from django.urls import path

from chart import views

app_name = "chart"

urlpatterns = [
    path("", views.home, name="home"),
    path(
        "api/candles/<str:symbol>/<str:interval>/",
        views.candles_api,
        name="candles",
    ),
    path(
        "api/refresh/<str:symbol>/<str:interval>/",
        views.refresh_api,
        name="refresh",
    ),
    path("api/oi/<str:symbol>/<str:period>/", views.oi_api, name="oi"),
    path("api/funding/<str:symbol>/", views.funding_api, name="funding"),
    path("api/cvd/<str:symbol>/<str:interval>/", views.cvd_api, name="cvd"),
    path("api/clusters/<str:symbol>/", views.clusters_api, name="clusters"),
]
