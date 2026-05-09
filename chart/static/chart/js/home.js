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

        async function refresh() {
            const myId = ++lastRequestId;
            refreshBtn.disabled = true;
            setLoading(true);
            setStatus("Refreshing…");
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
                const r = json.result || {};
                setStatus(
                    "Refreshed: created " +
                        (r.created || 0) +
                        ", updated " +
                        (r.updated || 0)
                );
            } catch (e) {
                if (myId !== lastRequestId) return;
                setStatus("Error: " + e.message, true);
            } finally {
                refreshBtn.disabled = false;
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
        });

        intervalSel.addEventListener("change", function () {
            current.interval = intervalSel.value;
            loadCandles();
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
        loadCandles();
    });
})();
