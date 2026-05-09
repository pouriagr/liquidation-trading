"""URL routes for the `chart` app.

Mounted at the project root in `core.urls` under namespace `chart`. Three
routes: the home page (HTML) and two JSON APIs that the home page's JS calls.
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
]
