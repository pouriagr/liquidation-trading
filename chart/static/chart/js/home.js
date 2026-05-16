/* Lightweight Charts wiring for the chart home page.
 *
 * Layout:
 *   - One chart instance mounted in #chart.
 *   - Candlestick series on the default price scale.
 *   - Volume histogram series overlaid on the bottom 20% (priceScaleId: ''
 *     + scaleMargins) — the standard Lightweight Charts volume sub-pane recipe.
 *
 * Data flow:
 *   - On load and on symbol/interval change: GET /api/candles/<sym>/<int>/
 *   - On Refresh click:                       POST /api/refresh/<sym>/<int>/
 *   Both responses share the same { candles: [...] } shape, so renderCandles()
 *   handles them identically.
 *
 * Loading state:
 *   - While a request is in flight, #chart gets the .is-loading class which
 *     dims the pane via a CSS overlay (cursor: progress on top).
 *   - A monotonically-increasing `lastRequestId` lets us drop late responses
 *     when the user clicks a different pair mid-load — newest click wins.
 *
 * CSRF: the home template renders {% csrf_token %} so Django sets the
 * `csrftoken` cookie. We read it with getCookie() and send X-CSRFToken on POST.
 */

(function () {
    "use strict";

    const UP = "#26a69a";
    const DOWN = "#ef5350";
    const VOL_UP = "rgba(38, 166, 154, 0.5)";
    const VOL_DOWN = "rgba(239, 83, 80, 0.5)";

    // Cluster overlay colour ramp — Coinglass-style heatmap intensity
    // gradient (purple → blue → cyan → yellow → red). Colour encodes
    // *strength*, not side (long_liq / short_liq); the side stays
    // available on the tooltip path. Five sRGB stops are sufficient for
    // a heat ramp at 8-bit translucent alpha — perceptual uniformity is
    // not a requirement here. Keep stops in lockstep with the CSS
    // gradient on `.overlay-legend-bar`; the legend mirrors this array.
    const CLUSTER_RAMP = [
        { p: 0.00, c: [0x3b, 0x0a, 0x45] },
        { p: 0.25, c: [0x2b, 0x4f, 0x8a] },
        { p: 0.50, c: [0x2b, 0xb4, 0xc4] },
        { p: 0.75, c: [0xf7, 0xc5, 0x48] },
        { p: 1.00, c: [0xef, 0x3b, 0x3b] },
    ];
    // Translucent fill so the candle wicks stay visible underneath the
    // band. 0.45 is the sweet spot in practice — opaque enough that a
    // narrow band reads, transparent enough that overlapping bands
    // don't compound into a solid wall.
    const CLUSTER_OPACITY = 0.45;

    // §5.5 recency decay half-life. Mirrors the server-side
    // `feature/services/clustering.py:RECENCY_HALFLIFE_HOURS = 72.0`
    // so the framework doc's single canonical curve covers both ends.
    // Anchor: each segment's *own* `source_time` — the right edge of
    // the backward-looking rolling lookback window that identified it.
    // Per-bin (not per-segment): brightness fades along the segment's
    // own lifetime, viewport-independent. See `_paint`.
    const RECENCY_HALFLIFE_HOURS = 72;
    const RECENCY_HALFLIFE_SEC = RECENCY_HALFLIFE_HOURS * 3600;

    // Heatmap cell pitch on the vertical (price) axis — % of anchor.
    // 0.005 matches the server-side `PRICE_BAND_PCT` so each source
    // segment maps to exactly one heatmap row; if the server bin
    // shrinks, neighbouring source bands collapse into one row, which
    // is also fine (just less granular). Keep this and
    // `ClusterIdentifierController.PRICE_BAND_PCT` in lockstep when
    // either is tuned.
    const HEATMAP_PRICE_PCT = 0.005;

    // Time-bin sizes per chart interval. Heatmap cells are this wide
    // horizontally, so each column lines up with one candle. Values are
    // exactly the Binance interval cadence (no rounding).
    const HEATMAP_INTERVAL_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
        "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
        "1M": 2592000,
    };

    // Offset for the `priceBin` half of the (timeBin, priceBin) cell
    // key. `priceBin` can be negative (prices below anchor); shifting
    // by 2^19 lets us encode all realistic bins as positive integers
    // before packing into the low half of the key. 2^19 ≈ 524k slots
    // either side of anchor — at 0.5% pitch that's ±2 621× anchor,
    // far beyond any plausible symbol range.
    const HEATMAP_PRICE_BIN_OFFSET = 1 << 19;
    // High half of the encoded cell key; `timeBin << 20` reserves the
    // bottom 20 bits for `priceBin + offset`. Number safety: typical
    // `timeBin` values are O(2×10⁶) (15 m bins over decades), so
    // `timeBin × 2²⁰ ≈ 2×10¹²` stays well inside
    // `Number.MAX_SAFE_INTEGER ≈ 9×10¹⁵`.
    const HEATMAP_KEY_TIME_MULT = 1 << 20;

    // sRGB-linear interpolation between adjacent ramp stops.
    // `p` is clamped to [0, 1]; out-of-range inputs land on the
    // endpoints rather than throwing — strength percentile can drift
    // a hair outside the range on the boundary stops.
    function rampColor(p) {
        if (!(p >= 0)) p = 0;          // catches NaN and negatives
        if (p > 1) p = 1;
        for (let i = 1; i < CLUSTER_RAMP.length; i++) {
            const hi = CLUSTER_RAMP[i];
            if (p <= hi.p) {
                const lo = CLUSTER_RAMP[i - 1];
                const span = hi.p - lo.p || 1;
                const t = (p - lo.p) / span;
                const r = Math.round(lo.c[0] + (hi.c[0] - lo.c[0]) * t);
                const g = Math.round(lo.c[1] + (hi.c[1] - lo.c[1]) * t);
                const b = Math.round(lo.c[2] + (hi.c[2] - lo.c[2]) * t);
                return "rgba(" + r + "," + g + "," + b + "," + CLUSTER_OPACITY + ")";
            }
        }
        const last = CLUSTER_RAMP[CLUSTER_RAMP.length - 1].c;
        return "rgba(" + last[0] + "," + last[1] + "," + last[2] + "," + CLUSTER_OPACITY + ")";
    }

    // The chart's Refresh button orchestrates a multi-source fetch+backfill
    // bundle tied to the framework's 15m decision rhythm (see
    // docs/liquidation_framework_concept.md §12.3). Disable it for other
    // intervals so the user can't trigger work the server will reject.
    const REFRESH_INTERVAL = "15m";

    // Default visible window on initial load, symbol switch, interval
    // change, and right-click "Reset chart view". One week of
    // wall-clock time is roomy enough to show structure on every
    // interval (672 bars at 15m, ~7 bars at 1d) and narrow enough that
    // the cluster heatmap's per-paint aggregation stays cheap even at
    // year-scale data — `fitContent()` over a year of 15m candles
    // would put ~35 000 cluster bins in the viewport and grind to a
    // halt. User pan/zoom from this starting point is unbounded.
    const DEFAULT_VISIBLE_WINDOW_SEC = 7 * 24 * 3600;

    // --- helpers ----------------------------------------------------------
    function getCookie(name) {
        const prefix = name + "=";
        const parts = document.cookie ? document.cookie.split("; ") : [];
        for (const part of parts) {
            if (part.startsWith(prefix)) {
                return decodeURIComponent(part.slice(prefix.length));
            }
        }
        return null;
    }

    function buildUrl(kind, symbol, interval) {
        return window.CHART_URLS[kind]
            .replace("__SYM__", encodeURIComponent(symbol))
            .replace("__INT__", encodeURIComponent(interval));
    }

    // --- chart bootstrap --------------------------------------------------
    document.addEventListener("DOMContentLoaded", function () {
        const container = document.getElementById("chart");
        const statusEl = document.getElementById("status");
        const refreshBtn = document.getElementById("refresh");
        const intervalSel = document.getElementById("interval");
        const symbolsNav = document.getElementById("symbols");

        const current = {
            symbol: window.CHART_INITIAL.symbol,
            interval: window.CHART_INITIAL.interval,
        };

        // Monotonic request id — used to ignore stale responses when the user
        // clicks a different symbol/interval before the previous fetch returns.
        let lastRequestId = 0;

        const chart = LightweightCharts.createChart(container, {
            width: container.clientWidth,
            height: container.clientHeight,
            layout: {
                background: { type: "solid", color: "#131722" },
                textColor: "#d1d4dc",
            },
            grid: {
                vertLines: { color: "#1e222d" },
                horzLines: { color: "#1e222d" },
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: "#2a2e39" },
            timeScale: {
                borderColor: "#2a2e39",
                timeVisible: true,
                secondsVisible: false,
                // Pin the visible range to the data on both ends.
                // Without this, dragging past the first/last bar lets
                // the chart "rubber-band" — the visible range silently
                // narrows during the over-pull, which reads as an
                // accidental zoom-in at the edge.
                fixLeftEdge: true,
                fixRightEdge: true,
            },
            // Zoom is wheel-only. Dragging on either axis (the default
            // "press + move = zoom" gesture) is disabled so a pan never
            // accidentally rescales the chart — only the mouse wheel
            // (and pinch on touch devices) zooms. Drag inside the chart
            // area still pans, via the default `handleScroll`.
            handleScale: {
                axisPressedMouseMove: { time: false, price: false },
                mouseWheel: true,
                pinch: true,
            },
        });

        const candleSeries = chart.addCandlestickSeries({
            upColor: UP,
            downColor: DOWN,
            borderVisible: false,
            wickUpColor: UP,
            wickDownColor: DOWN,
        });

        // Volume on its own overlay scale, anchored to the bottom 20% of the chart.
        const volumeSeries = chart.addHistogramSeries({
            priceFormat: { type: "volume" },
            priceScaleId: "",
            color: VOL_UP,
        });
        volumeSeries.priceScale().applyOptions({
            scaleMargins: { top: 0.8, bottom: 0 },
        });

        // Lightweight Charts is not auto-responsive — wire it ourselves.
        new ResizeObserver(function () {
            chart.applyOptions({
                width: container.clientWidth,
                height: container.clientHeight,
            });
        }).observe(container);

        // --- data loading -------------------------------------------------
        function setStatus(text, isError) {
            statusEl.textContent = text || "";
            statusEl.classList.toggle("is-error", !!isError);
        }

        function setLoading(on) {
            container.classList.toggle("is-loading", !!on);
        }

        function renderCandles(json) {
            const candles = json.candles;
            candleSeries.setData(candles);
            volumeSeries.setData(
                candles.map(function (c) {
                    return {
                        time: c.time,
                        value: c.volume,
                        color: c.close >= c.open ? VOL_UP : VOL_DOWN,
                    };
                })
            );
            applyDefaultVisibleRange(candles);
        }

        // Set the chart's visible range to the trailing
        // `DEFAULT_VISIBLE_WINDOW_SEC` of data, clamped to whatever
        // candles actually exist. Used after every data load and by
        // the right-click reset menu. Called with an empty list it's
        // a no-op — fitContent would no-op too, the chart just stays
        // blank until candles arrive.
        function applyDefaultVisibleRange(candles) {
            if (!candles || candles.length === 0) return;
            const lastTime = candles[candles.length - 1].time;
            const firstAvailable = candles[0].time;
            const targetFrom = lastTime - DEFAULT_VISIBLE_WINDOW_SEC;
            const from = Math.max(firstAvailable, targetFrom);
            chart.timeScale().setVisibleRange({ from: from, to: lastTime });
        }

        async function loadCandles() {
            const myId = ++lastRequestId;
            setLoading(true);
            setStatus("Loading…");
            try {
                const resp = await fetch(
                    buildUrl("candles", current.symbol, current.interval),
                    { headers: { Accept: "application/json" } }
                );
                const json = await resp.json();
                if (myId !== lastRequestId) return; // newer click won — drop this paint
                if (!resp.ok) {
                    throw new Error(json.message || "Request failed");
                }
                renderCandles(json);
                setStatus(
                    json.fetched
                        ? "Fetched " + json.count + " candles"
                        : "Loaded " + json.count + " candles"
                );
            } catch (e) {
                if (myId !== lastRequestId) return;
                setStatus("Error: " + e.message, true);
            } finally {
                if (myId === lastRequestId) setLoading(false);
            }
        }

        // --- cluster overlay ---------------------------------------------
        // §5 liquidation clusters rendered as a 2D heatmap grid — one
        // colour per `(time_bin, price_bin)` cell. The TradingLite-style
        // visual: segments at the same price+time merge into a single
        // intensity block instead of stacking as translucent rectangles.
        //
        // Architecture: everything happens *inside `_paint`*, scoped to
        // the visible viewport. We don't pre-aggregate; an upfront pass
        // over a year of segments × their bin lifetime would expand
        // into millions of Map ops and freeze the browser on first
        // load (especially for open-ended segments touching 35 k+
        // bins each). Per-paint aggregation is bounded by viewport
        // size — typically ~200 bins × the active segments in that
        // window — and runs in single-digit milliseconds even on
        // year-scale history.
        //
        // Colour normalisation is *viewport-local*: the strongest cell
        // in the visible window gets red. As the user pans into a
        // quieter region, that region's local maximum becomes the new
        // "strongest". For backtest scrubbing this is more useful than
        // a global rank, where most cells outside the all-time hotspots
        // would appear faint.
        //
        // §5.5 recency is also applied here, but on a different axis:
        // each segment carries a `source_time` (= the right edge of
        // the rolling lookback window that identified it). Within a
        // segment's lifetime, brightness decays *per-bin* from that
        // anchor, so each band reads as a heat tail pointing from
        // bright-at-birth to dim-at-old-age. This is intentionally
        // viewport-independent — pan/zoom doesn't move the colour of
        // any cell, only which cells you can see. The right edge of
        // the chart is NOT automatically the hottest spot: most cells
        // there are bins far past the source_time of whatever segment
        // they belong to.
        const clustersToggle = document.getElementById("clusters-toggle");
        const clustersThreshold = document.getElementById("clusters-threshold");
        const clustersThresholdValue = document.getElementById("clusters-threshold-value");
        const clustersWindows = document.getElementById("clusters-windows");
        let clusterSegments = [];        // last fetched, full set
        let clusterAnchor = 0;           // anchor_price from payload (median segment price)
        let clusterStrengthsAsc = [];    // sorted *segment* strengths for the slider filter
        let clusterPrimitive = null;     // ISeriesPrimitive attached to candleSeries
        let clusterTickHandle = null;    // setInterval for wall-clock right-edge advance
        let lastClusterId = 0;           // monotonic, same reason as the candle/indicator ids
        // Multi-select lookback windows. Default = all three; the click
        // handler refuses to leave the set empty (the server returns 400
        // on `?windows=` too — belt-and-braces). Iteration order is the
        // insertion order so the URL string is stable across reloads.
        const selectedLookbacks = new Set([24, 72, 168]);

        function detachClusterPrimitive() {
            if (clusterPrimitive) {
                try {
                    candleSeries.detachPrimitive(clusterPrimitive);
                } catch (e) { /* noop — series already gone */ }
                clusterPrimitive = null;
            }
            if (clusterTickHandle) {
                clearInterval(clusterTickHandle);
                clusterTickHandle = null;
            }
        }

        function renderClusters(json) {
            detachClusterPrimitive();
            clusterSegments = (json && json.segments) || [];
            clusterAnchor = (json && json.anchor_price) || 0;
            clusterStrengthsAsc = clusterSegments
                .map(function (s) { return s.strength; })
                .sort(function (a, b) { return a - b; });
            if (clusterSegments.length === 0) return;
            clusterPrimitive = new ClusterBandPrimitive({
                getRampColor: rampColor,
            });
            candleSeries.attachPrimitive(clusterPrimitive);
            // Wall-clock advance for open-ended cells. `_paint` reads
            // `Date.now()` each frame, so a periodic `requestUpdate`
            // is the simplest way to keep the right-most cells in
            // sync with wall time. 60s is enough resolution for any
            // sensible chart cadence (15m candles or longer).
            clusterTickHandle = setInterval(function () {
                if (clusterPrimitive) clusterPrimitive.requestUpdate();
            }, 60000);
        }

        async function loadClusters() {
            // Always tear down first so an in-flight request whose
            // result we later discard doesn't leave a stale primitive
            // on the chart if the user toggles off mid-fetch.
            detachClusterPrimitive();
            if (!clustersToggle.checked) return;
            const myId = ++lastClusterId;
            // `?windows=24,72,168` (sorted-ascending subset) drives
            // multi-window aggregation server-side: the GET endpoint
            // sums strengths per (price_band, side) across the chosen
            // windows (§12.3 confluence via §5.4 sum). Default is all
            // three; the pill handler keeps the set non-empty.
            const windowsParam = Array.from(selectedLookbacks)
                .sort(function (a, b) { return a - b; })
                .join(",");
            const url = window.CHART_URLS.clusters
                .replace("__SYM__", encodeURIComponent(current.symbol))
                + "?windows=" + windowsParam;
            try {
                const resp = await fetch(url, {
                    headers: { Accept: "application/json" },
                });
                const json = await resp.json();
                if (myId !== lastClusterId) return;
                if (!resp.ok) {
                    throw new Error(json.message || "Clusters load failed");
                }
                renderClusters(json);
            } catch (e) {
                if (myId !== lastClusterId) return;
                setStatus("Clusters error: " + e.message, true);
            }
        }

        clustersToggle.addEventListener("change", function () {
            const on = clustersToggle.checked;
            clustersThreshold.disabled = !on;
            // Mirror the strength slider's disabled-when-overlay-off
            // behaviour for the lookback pills: visually communicate
            // that they're inert until the overlay is on.
            clustersWindows.querySelectorAll(".pill").forEach(function (p) {
                p.disabled = !on;
            });
            loadClusters();
        });

        // Lookback pill click handler — multi-select with a non-empty
        // floor. The last remaining pill cannot be toggled off; it
        // flashes red briefly to communicate the rejection. Server
        // also returns 400 on `?windows=`; this is the proximate UX.
        clustersWindows.addEventListener("click", function (e) {
            const btn = e.target.closest(".pill[data-lookback]");
            if (!btn || btn.disabled) return;
            const lb = parseInt(btn.dataset.lookback, 10);
            const isActive = btn.classList.contains("is-active");
            if (isActive && selectedLookbacks.size === 1) {
                btn.classList.add("pill--reject");
                setTimeout(function () {
                    btn.classList.remove("pill--reject");
                }, 240);
                return;
            }
            if (isActive) {
                selectedLookbacks.delete(lb);
                btn.classList.remove("is-active");
                btn.setAttribute("aria-pressed", "false");
            } else {
                selectedLookbacks.add(lb);
                btn.classList.add("is-active");
                btn.setAttribute("aria-pressed", "true");
            }
            if (clustersToggle.checked) loadClusters();
        });
        // rAF coalescing for the slider — a fast drag fires `input`
        // 60+ times per second, and each `requestUpdate` triggers a
        // full `_paint` (which may rebuild the cell cache if the
        // slider value moved enough to change the percentile cutoff).
        // Without throttling, drag → freeze. With it, drag → smooth
        // chart that updates at the screen refresh rate, never faster.
        let sliderRafScheduled = false;
        clustersThreshold.addEventListener("input", function () {
            // Label update must be synchronous — it's text the user is
            // reading mid-drag and doesn't depend on the heavy paint.
            clustersThresholdValue.textContent = "top " + clustersThreshold.value + "%";
            if (sliderRafScheduled) return;
            sliderRafScheduled = true;
            requestAnimationFrame(function () {
                sliderRafScheduled = false;
                if (clusterPrimitive) clusterPrimitive.requestUpdate();
            });
        });

        // ClusterBandPrimitive — implements the LWC v4 ISeriesPrimitive
        // contract. One pane view; the renderer walks the visible
        // segments and paints each as a translucent rectangle. Coordinate
        // conversions use the candle series for price-axis (because the
        // bands live in price space) and the chart's time scale for the
        // x-axis. Coinglass-style intensity colour comes from the
        // segment's strength percentile in the current payload.
        function ClusterBandPrimitive(opts) {
            this._opts = opts;
            this._chart = null;
            this._series = null;
            this._requestUpdate = null;
        }
        ClusterBandPrimitive.prototype.attached = function (param) {
            this._chart = param.chart;
            this._series = param.series;
            this._requestUpdate = param.requestUpdate;
        };
        ClusterBandPrimitive.prototype.detached = function () {
            this._chart = null;
            this._series = null;
            this._requestUpdate = null;
        };
        ClusterBandPrimitive.prototype.requestUpdate = function () {
            if (this._requestUpdate) this._requestUpdate();
        };
        ClusterBandPrimitive.prototype.updateAllViews = function () {
            // No per-view state to refresh; the renderer reads the
            // closure on every draw. Required by the ISeriesPrimitive
            // interface — LWC calls it after data changes.
        };
        ClusterBandPrimitive.prototype.paneViews = function () {
            const self = this;
            return [{
                // zOrder controls where the rectangles sit in the
                // candle-pane paint order. "bottom" puts them under the
                // candles so wicks always read clearly through the
                // translucent fill — matching Coinglass, where the
                // heatmap is the backdrop and price action is on top.
                zOrder: function () { return "bottom"; },
                renderer: function () {
                    return {
                        draw: function (target) {
                            target.useBitmapCoordinateSpace(function (scope) {
                                self._paint(scope);
                            });
                        },
                    };
                },
            }];
        };
        ClusterBandPrimitive.prototype._paint = function (scope) {
            if (!this._chart || !this._series) return;
            if (clusterSegments.length === 0) return;
            const timeScale = this._chart.timeScale();
            const visible = timeScale.getVisibleRange();
            if (!visible) return;

            // EVERYTHING runs inline per paint, scoped to the visible
            // viewport. No upfront expansion of segments into per-bin
            // cells (which used to freeze the browser for several
            // seconds when long-lived open-ended segments multiplied
            // by their lifetime in bins reached millions of Map ops).
            // For a ~200-bin visible window the work caps at
            // O(segments × bins_per_segment_in_viewport), which is
            // bounded by the viewport — never by total chart history.

            const ctx = scope.context;
            const hpr = scope.horizontalPixelRatio;
            const vpr = scope.verticalPixelRatio;
            const rightEdgePx = scope.bitmapSize.width;
            const series = this._series;
            const anchor = clusterAnchor > 0 ? clusterAnchor : 1;
            const timeBinSec = HEATMAP_INTERVAL_SECONDS[current.interval] || 900;
            const nowSec = Math.floor(Date.now() / 1000);
            const pricePct = HEATMAP_PRICE_PCT;
            const KEY_MULT = HEATMAP_KEY_TIME_MULT;
            const BIN_OFFSET = HEATMAP_PRICE_BIN_OFFSET;

            const fromSec = Number(visible.from);
            const toSec = Number(visible.to);
            const firstBin = Math.floor(fromSec / timeBinSec) - 1;
            const lastBin = Math.floor(toSec / timeBinSec) + 1;
            const visStartSec = firstBin * timeBinSec;
            const visEndSec = (lastBin + 1) * timeBinSec;

            // Strength cutoff for the "top X%" slider. Pre-computed
            // from the *segment* distribution at fetch time so this is
            // an O(1) lookup, not a sort per paint.
            const topPct = parseInt(clustersThreshold.value, 10) / 100;
            let cutoff = -Infinity;
            if (topPct < 1 && clusterStrengthsAsc.length > 0) {
                const idx = Math.floor((1 - topPct) * clusterStrengthsAsc.length);
                cutoff = clusterStrengthsAsc[idx];
            }

            // ---- Aggregation pass --------------------------------
            // For each segment that overlaps the visible window AND
            // passes the slider filter, expand it into its visible
            // bins only (clamped to [firstBin, lastBin]). Accumulate
            // into a flat Map keyed by `tb * KEY_MULT + priceBin`,
            // multiplying each bin's contribution by its §5.5 recency
            // weight — `exp(-(binTime − source_time) · ln2 / halflife)`
            // — so brightness fades along the segment's own lifetime.
            // Pre-compute the decay constant; the inner loop just does
            // one `exp` per bin.
            const decayConst = Math.LN2 / RECENCY_HALFLIFE_SEC;
            const cells = new Map();
            for (let i = 0; i < clusterSegments.length; i++) {
                const s = clusterSegments[i];
                if (s.strength < cutoff) continue;
                const endT = s.end_time != null ? s.end_time : nowSec;
                if (s.start_time >= visEndSec || endT < visStartSec) continue;

                const midPrice = (s.price_low + s.price_high) / 2;
                const rawPb = Math.floor((midPrice / anchor - 1) / pricePct);
                const priceBin = rawPb + BIN_OFFSET;
                if (priceBin < 0 || priceBin >= KEY_MULT) continue;

                let startBin = Math.floor(s.start_time / timeBinSec);
                if (startBin < firstBin) startBin = firstBin;
                let endBin = Math.floor(endT / timeBinSec);
                if (endBin > lastBin) endBin = lastBin;
                if (endBin < startBin) continue;

                const strength = s.strength;
                const sourceT = s.source_time;
                for (let tb = startBin; tb <= endBin; tb++) {
                    // Bin's left edge in unix seconds — the natural
                    // "moment" of the bin, matching how it's painted
                    // (`x1 = timeToCoordinate(tb * timeBinSec)` below).
                    // `max(0, …)` guards the first bin of a segment
                    // whose `source_time` isn't aligned to the bin
                    // grid: floor-aligning the bin can put its left
                    // edge a few seconds *before* source_time, which
                    // would otherwise produce a tiny negative age and
                    // a brightness >1.
                    const binTime = tb * timeBinSec;
                    const ageSec = binTime > sourceT ? binTime - sourceT : 0;
                    const decay = Math.exp(-ageSec * decayConst);
                    const key = tb * KEY_MULT + priceBin;
                    cells.set(key, (cells.get(key) || 0) + strength * decay);
                }
            }
            if (cells.size === 0) return;

            // ---- Colour-ranking distribution ---------------------
            // Sorted strengths over JUST the visible cells. As the
            // user pans, the strongest cell in the visible window
            // gets red — relative-to-viewport feels more useful for
            // a backtest scrub than a global rank that washes out
            // most of the chart most of the time.
            const strengthsAsc = Array.from(cells.values()).sort(function (a, b) {
                return a - b;
            });
            const nStrengths = strengthsAsc.length;

            // ---- Paint pass --------------------------------------
            // One filled rectangle per populated cell. Off-screen
            // cells (e.g. price outside visible range) return null
            // from priceToCoordinate and get culled.
            const rampColor = this._opts.getRampColor;
            for (const [key, strength] of cells) {
                const tb = Math.floor(key / KEY_MULT);
                const priceBin = (key - tb * KEY_MULT) - BIN_OFFSET;

                const t0 = tb * timeBinSec;
                const x1 = timeScale.timeToCoordinate(t0);
                if (x1 === null) continue;
                const x2 = timeScale.timeToCoordinate(t0 + timeBinSec);
                const x1Px = x1 * hpr;
                const x2Px = x2 === null ? rightEdgePx : x2 * hpr;
                if (x2Px <= x1Px) continue;
                const w = Math.max(1, x2Px - x1Px);

                const priceLo = anchor * (1 + priceBin * pricePct);
                const priceHi = anchor * (1 + (priceBin + 1) * pricePct);
                const yHi = series.priceToCoordinate(priceHi);
                const yLo = series.priceToCoordinate(priceLo);
                if (yHi === null || yLo === null) continue;
                const top = Math.min(yHi * vpr, yLo * vpr);
                const h = Math.max(1, Math.abs(yLo * vpr - yHi * vpr));

                // Inline binary search — cheaper than a function
                // call per cell when we paint thousands.
                let lo = 0, hi = nStrengths;
                while (lo < hi) {
                    const mid = (lo + hi) >>> 1;
                    if (strengthsAsc[mid] < strength) lo = mid + 1;
                    else hi = mid;
                }
                const p = nStrengths === 1 ? 1 : lo / (nStrengths - 1);
                ctx.fillStyle = rampColor(p);
                ctx.fillRect(x1Px, top, w, h);
            }
        };

        // --- indicator sub-pane ------------------------------------------
        // A second Lightweight Charts instance, lazily created on first
        // selection. Its time axis is independent from the candle pane
        // by design — per spec, the user wants to pan/zoom the indicator
        // separately. We do NOT call any cross-chart sync helper.
        const indicatorEl = document.getElementById("indicator-chart");
        let indicatorChart = null;
        let indicatorSeries = null;   // the active series instance, or null
        let activeIndicator = "";     // "" | "oi:5m" | "oi:1h" | "funding" | "cvd:5m" | "cvd:15m"
        // Monotonic id for indicator fetches — separate from the candle
        // pane's `lastRequestId` so the two panes don't invalidate each
        // other's in-flight requests.
        let lastIndicatorId = 0;

        function setIndicatorLoading(on) {
            indicatorEl.classList.toggle("is-loading", !!on);
        }

        function showIndicatorPane() {
            indicatorEl.classList.remove("is-hidden");
            // The pane was display:none until now, so the chart was created
            // (or last sized) against zero dimensions — re-apply explicit
            // sizing once it has a real box.
            if (indicatorChart) {
                indicatorChart.applyOptions({
                    width: indicatorEl.clientWidth,
                    height: indicatorEl.clientHeight,
                });
            }
        }

        function hideIndicatorPane() {
            indicatorEl.classList.add("is-hidden");
        }

        function ensureIndicatorChart() {
            if (indicatorChart) return indicatorChart;
            // Reveal the pane first so clientWidth/Height are non-zero at
            // the moment createChart() snapshots them.
            indicatorEl.classList.remove("is-hidden");
            indicatorChart = LightweightCharts.createChart(indicatorEl, {
                width: indicatorEl.clientWidth,
                height: indicatorEl.clientHeight,
                layout: {
                    background: { type: "solid", color: "#131722" },
                    textColor: "#d1d4dc",
                },
                grid: {
                    vertLines: { color: "#1e222d" },
                    horzLines: { color: "#1e222d" },
                },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor: "#2a2e39" },
                timeScale: {
                    borderColor: "#2a2e39",
                    timeVisible: true,
                    secondsVisible: false,
                    // Match the price chart: no edge over-pull, which
                    // would otherwise pump narrowed ranges through the
                    // time-sync link below and look like a zoom on the
                    // other pane.
                    fixLeftEdge: true,
                    fixRightEdge: true,
                },
                // Match the price chart: wheel-only zoom, no axis-drag
                // zoom. Otherwise the two panes would feel inconsistent
                // and a drag here would re-trigger zoom syncing too.
                handleScale: {
                    axisPressedMouseMove: { time: false, price: false },
                    mouseWheel: true,
                    pinch: true,
                },
            });
            new ResizeObserver(function () {
                indicatorChart.applyOptions({
                    width: indicatorEl.clientWidth,
                    height: indicatorEl.clientHeight,
                });
            }).observe(indicatorEl);

            // Link the two time scales so panning/zooming one drags the
            // other along. Time-range (not logical-range) sync, because
            // the two panes can have different bar counts (e.g. 15m
            // candles vs hourly OI) and the user expects "same time
            // window everywhere", not "same bar indices everywhere".
            //
            // The `syncingTime` guard breaks the feedback loop —
            // without it, A's change fires B's listener, which sets B's
            // range, which fires A's listener, which … ping-pong forever.
            let syncingTime = false;
            function linkTimeRange(from, to) {
                from.timeScale().subscribeVisibleTimeRangeChange(function (range) {
                    if (syncingTime || !range) return;
                    syncingTime = true;
                    try {
                        to.timeScale().setVisibleRange(range);
                    } finally {
                        syncingTime = false;
                    }
                });
            }
            linkTimeRange(chart, indicatorChart);
            linkTimeRange(indicatorChart, chart);

            return indicatorChart;
        }

        function buildIndicatorUrl(kind, key) {
            // `oi` and `cvd` both use the `__INT__` placeholder for their
            // second segment (period / interval); `funding` has none.
            const tmpl = window.CHART_URLS[kind];
            let url = tmpl.replace("__SYM__", encodeURIComponent(current.symbol));
            if (key !== undefined) {
                url = url.replace("__INT__", encodeURIComponent(key));
            }
            return url;
        }

        function renderIndicator(kind, json) {
            const c = ensureIndicatorChart();
            // Strip any previous series before installing the new one —
            // otherwise switching e.g. OI → CVD leaves the OI line still
            // drawn underneath.
            if (indicatorSeries) {
                c.removeSeries(indicatorSeries);
                indicatorSeries = null;
            }
            if (kind === "funding") {
                indicatorSeries = c.addHistogramSeries({
                    priceFormat: {
                        type: "price",
                        precision: 6,
                        minMove: 0.000001,
                    },
                });
            } else if (kind === "oi") {
                indicatorSeries = c.addLineSeries({
                    color: "#5c9eff",
                    lineWidth: 2,
                    priceFormat: { type: "volume" },
                });
            } else {
                // cvd
                indicatorSeries = c.addLineSeries({
                    color: "#f5a623",
                    lineWidth: 2,
                });
            }
            indicatorSeries.setData(json.points);
            c.timeScale().fitContent();
        }

        async function loadIndicator(value) {
            if (!value) {
                // Hide the pane but don't tear down `indicatorChart` —
                // keep it warm so the next selection reuses it. The
                // series is removed in renderIndicator() at next show.
                hideIndicatorPane();
                return;
            }
            const myId = ++lastIndicatorId;
            const [kind, key] = value.split(":");
            const url = buildIndicatorUrl(kind, key);

            ensureIndicatorChart();         // also unhides the pane
            setIndicatorLoading(true);
            try {
                const resp = await fetch(url, {
                    headers: { Accept: "application/json" },
                });
                const json = await resp.json();
                if (myId !== lastIndicatorId) return;  // newer pick won
                if (!resp.ok) {
                    throw new Error(json.message || "Indicator load failed");
                }
                renderIndicator(kind, json);
                showIndicatorPane();
            } catch (e) {
                if (myId !== lastIndicatorId) return;
                setStatus("Indicator error: " + e.message, true);
            } finally {
                if (myId === lastIndicatorId) setIndicatorLoading(false);
            }
        }

        // Centralise the off-15m gate: keep the button disabled (with a
        // tooltip explaining why) for any interval the server would
        // reject anyway.
        function updateRefreshAvailability() {
            const allowed = current.interval === REFRESH_INTERVAL;
            refreshBtn.disabled = !allowed;
            refreshBtn.title = allowed
                ? "Refresh the 15m trading bundle (candles 5m/15m/4h/1d, OI 5m+derived 1h, funding)"
                : "Refresh is only available at " + REFRESH_INTERVAL +
                  " — the framework's decision timeframe.";
        }

        function formatSource(s) {
            // "candles 15m: +120/~3 (backfilled)" or
            // "funding: ⚠ RequestException: ..."
            if (s.error) {
                return s.label + ": ⚠ " + s.error;
            }
            return (
                s.label +
                ": +" + (s.created || 0) +
                "/~" + (s.updated || 0) +
                (s.backfilled ? " (backfilled)" : "")
            );
        }

        async function refresh() {
            if (current.interval !== REFRESH_INTERVAL) {
                // Defensive — button should be disabled, but a keyboard
                // shortcut or stale UI state could still get here.
                setStatus(
                    "Refresh is only available at " + REFRESH_INTERVAL + ".",
                    true
                );
                return;
            }

            const myId = ++lastRequestId;
            refreshBtn.disabled = true;
            setLoading(true);
            // First-time refresh on an empty DB triggers backfills across
            // four candle TFs + OI metrics + a year of funding. Be honest
            // about how long that takes so the user doesn't think it hung.
            setStatus("Refreshing (may take several minutes on first run)…");
            try {
                const resp = await fetch(
                    buildUrl("refresh", current.symbol, current.interval),
                    {
                        method: "POST",
                        headers: {
                            Accept: "application/json",
                            "X-CSRFToken": getCookie("csrftoken") || "",
                        },
                    }
                );
                const json = await resp.json();
                if (myId !== lastRequestId) return;
                if (!resp.ok) {
                    throw new Error(json.message || "Refresh failed");
                }
                renderCandles(json);

                // Refresh just ingested fresh 1h OI + 5m candles, which
                // are exactly the inputs to the cluster identifier — so
                // re-fetch the overlay when it's on, otherwise the user
                // sees the stale (pre-refresh) bands until the next
                // symbol switch or toggle. Independent of `renderCandles`
                // because the overlay's data path is its own endpoint.
                if (clustersToggle.checked) loadClusters();

                const sources = (json.refresh && json.refresh.sources) || [];
                const anyError = sources.some(function (s) { return s.error; });
                const summary = sources.length
                    ? "Refresh OK · " + sources.map(formatSource).join(" · ")
                    : "Refresh OK";
                setStatus(summary, anyError);
            } catch (e) {
                if (myId !== lastRequestId) return;
                setStatus("Error: " + e.message, true);
            } finally {
                // Re-evaluate gating after the run — user may have changed
                // interval mid-flight, or we may have errored at 15m.
                updateRefreshAvailability();
                if (myId === lastRequestId) setLoading(false);
            }
        }

        // --- wiring -------------------------------------------------------
        symbolsNav.addEventListener("click", function (ev) {
            const pill = ev.target.closest(".pill");
            if (!pill || !symbolsNav.contains(pill)) return;
            const sym = pill.dataset.symbol;
            if (!sym || sym === current.symbol) return;
            symbolsNav
                .querySelectorAll(".pill.is-active")
                .forEach(function (el) {
                    el.classList.remove("is-active");
                });
            pill.classList.add("is-active");
            current.symbol = sym;
            loadCandles();
            // Indicator follows the symbol — re-fetch under the new pair
            // if one is currently active. (interval is deliberately *not*
            // tied to anything here; that's baked into `activeIndicator`.)
            if (activeIndicator) {
                loadIndicator(activeIndicator);
            }
            // Cluster overlay also follows the symbol. The toggle state
            // is preserved so the user doesn't have to re-enable it
            // every time they switch pairs.
            if (clustersToggle.checked) {
                loadClusters();
            }
        });

        intervalSel.addEventListener("change", function () {
            current.interval = intervalSel.value;
            updateRefreshAvailability();
            loadCandles();
            // The cluster heatmap's time-bin width depends on the
            // current chart interval (see `HEATMAP_INTERVAL_SECONDS`).
            // Force a primitive repaint so `_paint` re-aggregates at
            // the new bin.
            if (clusterPrimitive) clusterPrimitive.requestUpdate();
            // Note: we intentionally do NOT reload the indicator here.
            // The indicator's interval is part of `activeIndicator`
            // (e.g. "cvd:5m"); the candle pane's interval is unrelated.
        });

        // Radio handler — single-select for the indicator. The `value`
        // attribute is the "kind:key" string `loadIndicator` parses.
        const indicatorsNav = document.getElementById("indicators");
        indicatorsNav.addEventListener("change", function (ev) {
            if (ev.target.name !== "indicator") return;
            activeIndicator = ev.target.value;
            loadIndicator(activeIndicator);
        });

        refreshBtn.addEventListener("click", refresh);

        // --- right-click context menu ------------------------------------
        const menuEl = document.getElementById("chart-context-menu");
        const resetBtn = menuEl.querySelector('[data-action="reset-view"]');

        function showMenu(x, y) {
            menuEl.style.left = x + "px";
            menuEl.style.top = y + "px";
            menuEl.classList.add("is-open");
            menuEl.setAttribute("aria-hidden", "false");
            // flip if it would overflow the viewport
            const r = menuEl.getBoundingClientRect();
            if (r.right > window.innerWidth) {
                menuEl.style.left = Math.max(0, x - r.width) + "px";
            }
            if (r.bottom > window.innerHeight) {
                menuEl.style.top = Math.max(0, y - r.height) + "px";
            }
        }

        function hideMenu() {
            if (!menuEl.classList.contains("is-open")) return;
            menuEl.classList.remove("is-open");
            menuEl.setAttribute("aria-hidden", "true");
        }

        container.addEventListener("contextmenu", function (ev) {
            ev.preventDefault();
            showMenu(ev.clientX, ev.clientY);
        });

        resetBtn.addEventListener("click", function () {
            // Reset to the same default window as initial load — not
            // `fitContent()`, which over a year of 15m data puts ~35 k
            // cluster bins in the viewport and locks the browser.
            // `candleSeries.data()` is the LWC reader for the current
            // dataset; we feed it to the same helper the load path uses.
            const data = candleSeries.data ? candleSeries.data() : null;
            if (data && data.length > 0) {
                applyDefaultVisibleRange(data);
            }
            chart.priceScale("right").applyOptions({ autoScale: true });
            hideMenu();
        });

        document.addEventListener("mousedown", function (ev) {
            if (!menuEl.contains(ev.target)) hideMenu();
        });
        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") hideMenu();
        });
        container.addEventListener("wheel", hideMenu, { passive: true });
        window.addEventListener("blur", hideMenu);

        // initial load
        updateRefreshAvailability();
        loadCandles();
    });
})();
