from data.controllers.binance_candles import BinanceCandlesController
from data.controllers.binance_metrics_archive import BinanceMetricsArchiveController
from data.controllers.binance_open_interest import BinanceOpenInterestController

# Singletons — built once at import time, shared by every caller.
binance_candles_controller: BinanceCandlesController = BinanceCandlesController()
binance_open_interest_controller: BinanceOpenInterestController = (
    BinanceOpenInterestController()
)
binance_metrics_archive_controller: BinanceMetricsArchiveController = (
    BinanceMetricsArchiveController()
)

__all__ = [
    "BinanceCandlesController",
    "BinanceMetricsArchiveController",
    "BinanceOpenInterestController",
    "binance_candles_controller",
    "binance_metrics_archive_controller",
    "binance_open_interest_controller",
]
