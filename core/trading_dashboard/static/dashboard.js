/**
 * Trading Dashboard JS — Phase 17
 *
 * Handles data loading, Plotly chart rendering, SSE updates, and interactivity.
 */

(function () {
    'use strict';

    const BASE_URL = '/trading';
    let sse = null;
    let lastUpdate = null;

    // ─── Utility ────────────────────────────────────────────────────────

    function $(sel) { return document.querySelector(sel); }

    function $$(sel) { return document.querySelectorAll(sel); }

    function formatCurrency(val) {
        const sign = val >= 0 ? '+' : '';
        return sign + '$' + Number(val).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    function formatPct(val) {
        return Number(val).toFixed(1) + '%';
    }

    function formatDate(iso) {
        if (!iso) return '—';
        try {
            const d = new Date(iso);
            return d.toLocaleString();
        } catch (e) {
            return iso;
        }
    }

    function updateConnectionStatus(connected) {
        const dot = $('#connection-status');
        const label = $('#last-update');
        if (dot) {
            dot.className = 'status-dot ' + (connected ? 'connected' : 'disconnected');
        }
        if (label) {
            if (connected && lastUpdate) {
                label.textContent = 'Last update: ' + lastUpdate.toLocaleTimeString();
            } else if (connected) {
                label.textContent = 'Connected';
            } else {
                label.textContent = 'Disconnected — reconnecting...';
            }
        }
    }

    // ─── Data Loading ───────────────────────────────────────────────────

    async function fetchJSON(url) {
        const resp = await fetch(url);
        if (!resp.ok) {
            throw new Error('HTTP ' + resp.status + ' ' + resp.statusText);
        }
        return resp.json();
    }

    // ─── P&L Card Display ───────────────────────────────────────────────

    function renderPNL(data) {
        const el = $('#total-pnl');
        if (el) {
            const pnl = data.total_pnl || 0;
            el.textContent = formatCurrency(pnl);
            el.className = 'stat-value ' + (pnl >= 0 ? 'green' : 'red');
        }

        const bal = $('#current-balance');
        if (bal) {
            bal.textContent = formatCurrency(data.current_balance);
        }
    }

    function renderPNLChart(data) {
        const container = $('#pnl-chart');
        if (!container) return;

        const series = data.daily_series || [];
        if (series.length === 0) {
            container.innerHTML = '<div class="loading">No P&L data yet</div>';
            return;
        }

        const dates = series.map(s => s.date);
        const pnlVals = series.map(s => s.pnl);
        const cumulative = series.map(s => s.cumulative_pnl);

        const traces = [
            {
                x: dates,
                y: cumulative,
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Cumulative P&L',
                line: { color: '#3fb950', width: 2 },
                marker: { color: '#3fb950', size: 4 },
                fill: 'tozeroy',
                fillcolor: 'rgba(63, 185, 80, 0.08)',
            },
            {
                x: dates,
                y: pnlVals,
                type: 'bar',
                name: 'Daily P&L',
                marker: {
                    color: pnlVals.map(v => v >= 0 ? '#3fb950' : '#f85149'),
                    line: { width: 0 },
                },
                yaxis: 'y2',
            }
        ];

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#8b949e', size: 11 },
            margin: { l: 60, r: 60, t: 10, b: 40 },
            legend: { orientation: 'h', y: 1.1 },
            xaxis: {
                gridcolor: 'rgba(48, 54, 61, 0.5)',
                tickfont: { size: 10 },
            },
            yaxis: {
                title: 'Cumulative P&L ($)',
                gridcolor: 'rgba(48, 54, 61, 0.5)',
                tickfont: { size: 10 },
                zerolinecolor: '#30363d',
            },
            yaxis2: {
                title: 'Daily P&L ($)',
                overlaying: 'y',
                side: 'right',
                gridcolor: 'rgba(48, 54, 61, 0.5)',
                tickfont: { size: 10 },
                zerolinecolor: '#30363d',
                showgrid: false,
            },
            hovermode: 'x unified',
            dragmode: false,
        };

        Plotly.newPlot(container, traces, layout, {
            responsive: true,
            displayModeBar: false,
        });
    }

    function renderPNLByCity(data) {
        const container = $('#pnl-city-chart');
        if (!container) return;

        const byCity = data.by_city || {};
        const cities = Object.keys(byCity);
        if (cities.length === 0) {
            container.innerHTML = '<div class="loading">No city P&L data</div>';
            return;
        }

        const values = cities.map(c => byCity[c]);
        const colors = values.map(v => v >= 0 ? '#3fb950' : '#f85149');

        const traces = [{
            x: cities,
            y: values,
            type: 'bar',
            marker: { color: colors },
            text: values.map(v => formatCurrency(v)),
            textposition: 'outside',
            textfont: { size: 10 },
        }];

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#8b949e', size: 11 },
            margin: { l: 60, r: 20, t: 10, b: 60 },
            xaxis: {
                tickangle: -45,
                gridcolor: 'rgba(48, 54, 61, 0.5)',
                tickfont: { size: 10 },
            },
            yaxis: {
                title: 'P&L ($)',
                gridcolor: 'rgba(48, 54, 61, 0.5)',
                tickfont: { size: 10 },
                zerolinecolor: '#30363d',
            },
            hovermode: 'x',
            dragmode: false,
        };

        Plotly.newPlot(container, traces, layout, {
            responsive: true,
            displayModeBar: false,
        });
    }

    // ─── Portfolio ──────────────────────────────────────────────────────

    function renderPortfolio(data) {
        const el = $('#total-exposure');
        if (el) {
            el.textContent = formatCurrency(data.total_exposure);
            el.className = 'stat-value ' + (data.total_exposure > 0 ? 'yellow' : 'blue');
        }

        const container = $('#portfolio-cluster-chart');
        if (!container) return;

        const clusters = data.by_cluster || {};
        const labels = Object.keys(clusters);
        if (labels.length === 0) {
            container.innerHTML = '<div class="loading">No open positions</div>';
            return;
        }

        const values = labels.map(l => clusters[l]);
        const colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#f0883e'];

        const traces = [{
            labels: labels,
            values: values,
            type: 'pie',
            marker: {
                colors: colors.slice(0, labels.length),
                line: { color: '#1c2128', width: 2 },
            },
            textinfo: 'label+percent',
            textfont: { size: 11, color: '#e6edf3' },
            hole: 0.4,
            hoverinfo: 'label+value',
        }];

        const layout = {
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#8b949e', size: 11 },
            margin: { l: 20, r: 20, t: 10, b: 20 },
            showlegend: true,
            legend: { orientation: 'h', y: -0.2, font: { size: 10 } },
            dragmode: false,
        };

        Plotly.newPlot(container, traces, layout, {
            responsive: true,
            displayModeBar: false,
        });
    }

    // ─── Positions ──────────────────────────────────────────────────────

    function renderPositions(data) {
        const tbody = $('#positions-table-body');
        if (!tbody) return;

        const positions = data.positions || [];
        if (positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="11" class="loading">No open positions</td></tr>';
            return;
        }

        tbody.innerHTML = positions.map(p => {
            const unrealizedColor = p.unrealized_pnl >= 0 ? 'green' : 'red';
            const totalColor = p.total_pnl >= 0 ? 'green' : 'red';
            return `<tr>
                <td>${p.station}</td>
                <td>${p.market_side}</td>
                <td>${p.quantity}</td>
                <td>${p.average_cost}</td>
                <td>${p.market_price}</td>
                <td>${formatCurrency(p.mark_to_market_value)}</td>
                <td class="${p.realized_pnl >= 0 ? 'green' : 'red'}">${formatCurrency(p.realized_pnl)}</td>
                <td class="${unrealizedColor}">${formatCurrency(p.unrealized_pnl)}</td>
                <td class="${totalColor}">${formatCurrency(p.total_pnl)}</td>
                <td style="font-size: 11px; color: #8b949e;">${formatDate(p.opened_date_utc)}</td>
                <td><span class="badge badge-${p.status === 'active' ? 'green' : 'yellow'}">${p.status}</span></td>
            </tr>`;
        }).join('');

        const openEl = $('#open-positions');
        if (openEl) {
            openEl.textContent = data.open_count;
            openEl.className = 'stat-value ' + (data.open_count > 0 ? 'blue' : '');
        }
    }

    // ─── All Positions (full page) ──────────────────────────────────────

    function renderAllPositions(data) {
        const tbody = $('#all-positions-body');
        if (!tbody) return;

        const positions = data.positions || [];
        if (positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="13" class="loading">No positions found</td></tr>';
            return;
        }

        tbody.innerHTML = positions.map(p => {
            const totalColor = p.total_pnl >= 0 ? 'green' : 'red';
            const statusBadge = p.status === 'active' ? 'badge-green' :
                p.status === 'closed_partial' ? 'badge-yellow' : 'badge-red';
            return `<tr>
                <td style="font-size: 11px; font-family: monospace; color: #8b949e;">${p.position_uuid.substring(0, 8)}...</td>
                <td>${p.station}</td>
                <td>${p.market_type}</td>
                <td>${p.market_side}</td>
                <td>${p.quantity}</td>
                <td>${p.average_cost}</td>
                <td>${p.market_price}</td>
                <td>${formatCurrency(p.mark_to_market_value)}</td>
                <td class="${p.realized_pnl >= 0 ? 'green' : 'red'}">${formatCurrency(p.realized_pnl)}</td>
                <td class="${p.unrealized_pnl >= 0 ? 'green' : 'red'}">${formatCurrency(p.unrealized_pnl)}</td>
                <td class="${totalColor}">${formatCurrency(p.total_pnl)}</td>
                <td style="font-size: 11px; color: #8b949e;">${formatDate(p.opened_date_utc)}</td>
                <td><span class="badge ${statusBadge}">${p.status}</span></td>
            </tr>`;
        }).join('');
    }

    // ─── Alerts ─────────────────────────────────────────────────────────

    function renderAlerts(data) {
        const feed = $('#alert-feed');
        if (!feed) return;

        const alerts = data.alerts || [];
        if (alerts.length === 0) {
            feed.innerHTML = '<div class="loading">No alerts recorded</div>';
            return;
        }

        const stationFilter = ($('#alert-station-filter') || {}).value || '';
        const outcomeFilter = ($('#alert-outcome-filter') || {}).value || '';

        const filtered = alerts.filter(a => {
            if (stationFilter && !a.station.toUpperCase().includes(stationFilter.toUpperCase())) return false;
            if (outcomeFilter && a.outcome !== outcomeFilter) return false;
            return true;
        });

        feed.innerHTML = filtered.map(a => {
            const color = a.outcome_color || 'yellow';
            const edgeStr = a.edge != null ? 'edge=' + Number(a.edge).toFixed(3) : '';
            const confStr = a.confidence != null ? 'conf=' + Number(a.confidence).toFixed(2) : '';
            return `<div class="alert-row ${color}">
                <div class="flex items-center justify-between">
                    <span class="alert-station">${a.station}</span>
                    <span class="alert-time">${formatDate(a.timestamp_utc)}</span>
                </div>
                <div class="text-sm">
                    ${a.direction} ${a.market} — 
                    <span class="badge badge-${color}">${a.outcome}</span>
                    ${a.lane ? ' [' + a.lane + ']' : ''}
                    ${a.functionality ? ' · ' + a.functionality : ''}
                </div>
                <div class="text-sm text-muted">
                    ${edgeStr} ${confStr}
                    ${a.failure_mode ? ' · ' + a.failure_mode : ''}
                    ${a.position_size ? ' · size=' + formatCurrency(a.position_size) : ''}
                </div>
            </div>`;
        }).join('');

        if (filtered.length === 0) {
            feed.innerHTML = '<div class="loading">No alerts match filters</div>';
        }
    }

    // ─── Risk ───────────────────────────────────────────────────────────

    function renderRisk(data) {
        const indicator = $('#risk-indicator');
        const label = $('#risk-state-label');
        const reasons = $('#risk-reasons');

        if (indicator) {
            const state = (data.risk_state || 'OK').toLowerCase();
            indicator.textContent = state === 'kill_switch' ? 'KS' : state.substring(0, 3).toUpperCase();
            indicator.className = 'risk-indicator ' + state;
        }

        if (label) {
            label.textContent = 'State: ' + (data.risk_state || 'OK');
        }

        if (reasons) {
            const r = data.risk_reasons || [];
            reasons.textContent = r.length > 0 ? r.join('; ') : 'All clear';
        }

        // Drawdown
        const ddEl = $('#drawdown-value');
        if (ddEl) {
            const dd = data.drawdown_pct || 0;
            ddEl.textContent = formatPct(dd);
            ddEl.className = 'stat-value ' + (dd > 10 ? 'red' : dd > 5 ? 'yellow' : 'green');
        }

        const ddThermo = $('#drawdown-thermometer');
        const ddThreshold = data.drawdown_threshold || 15;
        if (ddThermo) {
            const pct = Math.min(100, (data.drawdown_pct || 0) / ddThreshold * 100);
            ddThermo.style.width = pct + '%';
            ddThermo.className = 'thermometer-fill ' + (pct > 80 ? 'danger' : pct > 50 ? 'warning' : 'safe');
        }

        const ddCurrent = $('#drawdown-current');
        if (ddCurrent) ddCurrent.textContent = (data.drawdown_pct || 0).toFixed(1);
        const ddMax = $('#drawdown-max');
        if (ddMax) ddMax.textContent = (data.max_drawdown_pct || 0).toFixed(1);
        const ddThresh = $('#drawdown-threshold');
        if (ddThresh) ddThresh.textContent = ddThreshold;

        // Daily P&L
        const dpnlEl = $('#daily-pnl-value');
        if (dpnlEl) {
            const dpnl = data.daily_pnl || 0;
            dpnlEl.textContent = formatCurrency(dpnl);
            dpnlEl.className = 'stat-value ' + (dpnl >= 0 ? 'green' : 'red');
        }

        const dpnlAmount = $('#daily-pnl-amount');
        if (dpnlAmount) dpnlAmount.textContent = formatCurrency(data.daily_pnl || 0);

        const dailyThermo = $('#daily-loss-thermometer');
        const dailyLimit = data.daily_loss_threshold || 500;
        if (dailyThermo) {
            const pct = Math.min(100, Math.abs(data.daily_pnl || 0) / dailyLimit * 100);
            dailyThermo.style.width = pct + '%';
            dailyThermo.className = 'thermometer-fill ' + (pct > 80 ? 'danger' : pct > 50 ? 'warning' : 'safe');
        }

        const dailyLimitEl = $('#daily-loss-limit');
        if (dailyLimitEl) dailyLimitEl.textContent = formatCurrency(dailyLimit);

        // Consecutive losses
        const consecEl = $('#consecutive-losses');
        if (consecEl) {
            const cl = data.consecutive_losses || 0;
            consecEl.textContent = cl;
            consecEl.className = 'stat-value ' + (cl >= 3 ? 'red' : cl > 0 ? 'yellow' : 'green');
        }
    }

    // ─── Stats ──────────────────────────────────────────────────────────

    function renderStats(data) {
        // Signal accuracy table
        const tbody = $('#signal-accuracy-body');
        if (tbody) {
            const acc = data.signal_accuracy || {};
            const keys = Object.keys(acc);
            if (keys.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="loading">No signal data yet</td></tr>';
            } else {
                tbody.innerHTML = keys.map(k => {
                    const s = acc[k];
                    const accColor = s.accuracy_pct >= 60 ? 'green' : s.accuracy_pct >= 45 ? 'yellow' : 'red';
                    return `<tr>
                        <td>${k}</td>
                        <td>${s.total}</td>
                        <td>${s.placed}</td>
                        <td>${s.wins}</td>
                        <td>${s.losses}</td>
                        <td class="${accColor}">${formatPct(s.accuracy_pct)}</td>
                    </tr>`;
                }).join('');
            }
        }

        // Win rate by station chart
        const wrContainer = $('#win-rate-chart');
        if (wrContainer) {
            const wr = data.win_rate_by_station || {};
            const stations = Object.keys(wr);
            if (stations.length === 0) {
                wrContainer.innerHTML = '<div class="loading">No settled trades yet</div>';
            } else {
                const winRates = stations.map(s => wr[s].win_rate_pct);
                const colors = winRates.map(v => v >= 50 ? '#3fb950' : '#f85149');
                const traces = [{
                    x: stations,
                    y: winRates,
                    type: 'bar',
                    marker: { color: colors },
                    text: winRates.map(v => formatPct(v)),
                    textposition: 'outside',
                    textfont: { size: 10 },
                }];
                const layout = {
                    paper_bgcolor: 'rgba(0,0,0,0)',
                    plot_bgcolor: 'rgba(0,0,0,0)',
                    font: { color: '#8b949e', size: 11 },
                    margin: { l: 50, r: 20, t: 10, b: 60 },
                    xaxis: { tickangle: -45, tickfont: { size: 10 }, gridcolor: 'rgba(48, 54, 61, 0.5)' },
                    yaxis: { title: 'Win Rate (%)', range: [0, 100], gridcolor: 'rgba(48, 54, 61, 0.5)', zerolinecolor: '#30363d' },
                    hovermode: 'x',
                    dragmode: false,
                    shapes: [{
                        type: 'line',
                        x0: -0.5, y0: 50, x1: stations.length - 0.5, y1: 50,
                        line: { color: 'rgba(210, 153, 34, 0.5)', width: 1, dash: 'dash' },
                    }],
                };
                Plotly.newPlot(wrContainer, traces, layout, {
                    responsive: true,
                    displayModeBar: false,
                });
            }
        }

        // Rolling Sharpe chart
        const sharpeContainer = $('#sharpe-chart');
        if (sharpeContainer) {
            const sharpe = data.rolling_sharpe || [];
            if (sharpe.length === 0) {
                sharpeContainer.innerHTML = '<div class="loading">Not enough trades for Sharpe calculation</div>';
            } else {
                const indices = sharpe.map(s => s.trade_index);
                const values = sharpe.map(s => s.sharpe);
                const traces = [{
                    x: indices,
                    y: values,
                    type: 'scatter',
                    mode: 'lines',
                    name: 'Rolling Sharpe (10-trade window)',
                    line: { color: '#bc8cff', width: 2 },
                    fill: 'tozeroy',
                    fillcolor: 'rgba(188, 140, 255, 0.08)',
                }];
                const layout = {
                    paper_bgcolor: 'rgba(0,0,0,0)',
                    plot_bgcolor: 'rgba(0,0,0,0)',
                    font: { color: '#8b949e', size: 11 },
                    margin: { l: 50, r: 20, t: 10, b: 40 },
                    xaxis: { title: 'Trade Index', gridcolor: 'rgba(48, 54, 61, 0.5)', tickfont: { size: 10 } },
                    yaxis: { title: 'Sharpe Ratio', gridcolor: 'rgba(48, 54, 61, 0.5)', tickfont: { size: 10 }, zerolinecolor: '#30363d' },
                    hovermode: 'x',
                    dragmode: false,
                    shapes: [
                        { type: 'line', x0: indices[0], y0: 1, x1: indices[indices.length - 1], y1: 1, line: { color: 'rgba(63, 185, 80, 0.4)', width: 1, dash: 'dash' } },
                        { type: 'line', x0: indices[0], y0: 0, x1: indices[indices.length - 1], y1: 0, line: { color: 'rgba(248, 81, 73, 0.4)', width: 1, dash: 'dash' } },
                    ],
                };
                Plotly.newPlot(sharpeContainer, traces, layout, {
                    responsive: true,
                    displayModeBar: false,
                });
            }
        }

        // Summary stats
        const summary = data.summary || {};
        const totalTradesEl = $('#stat-total-trades');
        if (totalTradesEl) totalTradesEl.textContent = summary.total_trades || 0;
        const winRateEl = $('#stat-win-rate');
        if (winRateEl) {
            const wr = summary.win_rate || 0;
            winRateEl.textContent = formatPct(wr);
            winRateEl.className = 'stat-value ' + (wr >= 50 ? 'green' : 'red');
        }
        const avgEdgeEl = $('#stat-avg-edge');
        if (avgEdgeEl) {
            const ae = summary.avg_edge || 0;
            avgEdgeEl.textContent = Number(ae).toFixed(4);
            avgEdgeEl.className = 'stat-value ' + (ae > 0 ? 'green' : 'red');
        }
    }

    // ─── Main Data Load ─────────────────────────────────────────────────

    async function loadAll() {
        try {
            const [pnl, positions, portfolio, alerts, risk, stats] = await Promise.all([
                fetchJSON(BASE_URL + '/api/pnl'),
                fetchJSON(BASE_URL + '/api/positions'),
                fetchJSON(BASE_URL + '/api/portfolio'),
                fetchJSON(BASE_URL + '/api/alerts?limit=50'),
                fetchJSON(BASE_URL + '/api/risk'),
                fetchJSON(BASE_URL + '/api/stats'),
            ]);

            renderPNL(pnl);
            renderPNLChart(pnl);
            renderPNLByCity(pnl);
            renderPositions(positions);
            renderPortfolio(portfolio);
            renderAlerts(alerts);
            renderRisk(risk);
            renderStats(stats);

            lastUpdate = new Date();
            updateConnectionStatus(true);
        } catch (e) {
            console.error('Dashboard load error:', e);
            updateConnectionStatus(false);
        }
    }

    // ─── Positions Page Data Load ───────────────────────────────────────

    async function loadPositions() {
        try {
            const [positions, risk, stats] = await Promise.all([
                fetchJSON(BASE_URL + '/api/positions'),
                fetchJSON(BASE_URL + '/api/risk'),
                fetchJSON(BASE_URL + '/api/stats'),
            ]);

            renderAllPositions(positions);
            renderRisk(risk);
            renderStats(stats);

            // Also load portfolio for exposure
            try {
                const portfolio = await fetchJSON(BASE_URL + '/api/portfolio');
                const el = $('#total-exposure');
                if (el) {
                    el.textContent = formatCurrency(portfolio.total_exposure);
                }
            } catch (e) { /* ignore */ }

            lastUpdate = new Date();
            updateConnectionStatus(true);
        } catch (e) {
            console.error('Positions load error:', e);
            updateConnectionStatus(false);
        }
    }

    // ─── SSE Setup ──────────────────────────────────────────────────────

    function setupSSE() {
        if (sse) {
            sse.close();
        }

        sse = new EventSource(BASE_URL + '/api/stream');

        sse.onmessage = function (event) {
            try {
                const data = JSON.parse(event.data);
                if (data.pnl) {
                    renderPNL(data.pnl);
                    renderPNLChart(data.pnl);
                    renderPNLByCity(data.pnl);
                }
                if (data.positions) {
                    renderPositions(data.positions);
                }
                if (data.portfolio) {
                    renderPortfolio(data.portfolio);
                }
                if (data.risk) {
                    renderRisk(data.risk);
                }
                if (data.stats) {
                    renderStats(data.stats);
                }

                lastUpdate = new Date();
                updateConnectionStatus(true);
            } catch (e) {
                console.error('SSE parse error:', e);
            }
        };

        sse.onerror = function () {
            updateConnectionStatus(false);
            // Browser will auto-reconnect
        };
    }

    // ─── Alert Filter Handlers ──────────────────────────────────────────

    function setupAlertFilters() {
        const stationInput = $('#alert-station-filter');
        const outcomeSelect = $('#alert-outcome-filter');

        if (stationInput) {
            stationInput.addEventListener('input', function () {
                // Re-fetch alerts with filters
                fetchJSON(BASE_URL + '/api/alerts?limit=50')
                    .then(renderAlerts)
                    .catch(e => console.error('Alert filter error:', e));
            });
        }
        if (outcomeSelect) {
            outcomeSelect.addEventListener('change', function () {
                fetchJSON(BASE_URL + '/api/alerts?limit=50')
                    .then(renderAlerts)
                    .catch(e => console.error('Alert filter error:', e));
            });
        }
    }

    // ─── Public API ─────────────────────────────────────────────────────

    window.loadDashboardData = function () {
        loadAll();
        setupSSE();
        setupAlertFilters();
        // Periodic refresh fallback
        setInterval(loadAll, 60000);
    };

    window.loadPositionsPage = function () {
        loadPositions();
        // SSE for positions page too
        setupSSE();
        setInterval(loadPositions, 60000);
    };

    // ─── Auto-init on any page ──────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        updateConnectionStatus(false);
    });

})();