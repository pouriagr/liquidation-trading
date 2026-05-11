from feature.controllers.cvd import CVDController
from feature.controllers.oi_aggregator import OIAggregatorController
from feature.controllers.refresh import RefreshController

# Singletons — built once at import time, shared by every caller.
cvd_controller: CVDController = CVDController()
oi_aggregator_controller: OIAggregatorController = OIAggregatorController()
# RefreshController takes `OIAggregatorController` via DI so a test can
# substitute a stub — production wiring is the real one.
refresh_controller: RefreshController = RefreshController(oi_aggregator=oi_aggregator_controller)

__all__ = [
    "CVDController",
    "OIAggregatorController",
    "RefreshController",
    "cvd_controller",
    "oi_aggregator_controller",
    "refresh_controller",
]
