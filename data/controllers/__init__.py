from data.controllers.binance_candles import BinanceCandlesController
from data.controllers.binance_open_interest import BinanceOpenInterestController

# Singletons — built once at import time, shared by every caller.
binance_candles_controller: BinanceCandlesController = BinanceCandlesController()
binance_open_interest_controller: BinanceOpenInterestController = (
    BinanceOpenInterestController()
)

__all__ = [
    "BinanceCandlesController",
    "BinanceOpenInterestController",
    "binance_candles_controller",
    "binance_open_interest_controller",
]
