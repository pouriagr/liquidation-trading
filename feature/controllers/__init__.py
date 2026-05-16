from feature.controllers.cluster_identifier import ClusterIdentifierController
from feature.controllers.cvd import CVDController
from feature.controllers.oi_aggregator import OIAggregatorController
from feature.controllers.refresh import RefreshController

# Singletons — built once at import time, shared by every caller.
cvd_controller: CVDController = CVDController()
oi_aggregator_controller: OIAggregatorController = OIAggregatorController()
# ClusterIdentifierController has no dependencies beyond ORM reads, so a
# plain construction here matches the CVD singleton's pattern. Built
# before `refresh_controller` so the latter can reuse this instance via
# DI — keeping the chart's GET path and the refresh's persist path on
# the same singleton (a test can substitute one and both layers see it).
cluster_identifier_controller: ClusterIdentifierController = ClusterIdentifierController()
# RefreshController takes its dependencies via DI so tests can swap
# stubs. Production wiring is the real singletons.
refresh_controller: RefreshController = RefreshController(
    oi_aggregator=oi_aggregator_controller,
    cluster_identifier=cluster_identifier_controller,
)

__all__ = [
    "ClusterIdentifierController",
    "CVDController",
    "OIAggregatorController",
    "RefreshController",
    "cluster_identifier_controller",
    "cvd_controller",
    "oi_aggregator_controller",
    "refresh_controller",
]
