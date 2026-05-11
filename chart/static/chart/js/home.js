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

    // The chart's Refresh button orchestrates a multi-source fetch+backfill
    // bundle tied to the framework's 15m decision rhythm (see
    // docs/liquidation_framework_concept.md §12.3). Disable it for other
    // intervals so the user can't trigger work the server will reject.
    const REFRESH_INTERVAL = "15m";

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
            chart.timeScale().fitContent();
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
        });

        intervalSel.addEventListener("change", function () {
            current.interval = intervalSel.value;
            updateRefreshAvailability();
            loadCandles();
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
            chart.timeScale().fitContent();
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
