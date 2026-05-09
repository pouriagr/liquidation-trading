from data.controllers.binance_candles import BinanceCandlesController

# Singletons — built once at import time, shared by every caller.
binance_candles_controller: BinanceCandlesController = BinanceCandlesController()

__all__ = [
    "BinanceCandlesController",
    "binance_candles_controller",
]
