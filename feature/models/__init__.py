"""Public surface of the feature app's models package.

`feature` holds derived signals computed from the raw rows in `data`.
At present `feature` itself defines no models — CVD is computed on
demand from `data.Candle.delta` rather than persisted (see
`feature.controllers.cvd.CVDController`). The package is kept so that
adding a model later (e.g. a slow-moving "cluster" cache) does not
require reshaping imports.
"""

__all__: list[str] = []
