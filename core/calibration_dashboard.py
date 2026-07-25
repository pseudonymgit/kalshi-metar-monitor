#!/usr/bin/env python3
"""
CALIBRATION DASHBOARD (v1.0) with UX Priority
Interactive dashboard for observing and managing weather trading model calibration metrics.
Combines metrics from:
- Paper trading performance
- Split backtest results  
- Calibration statistics (Brier, ECE, etc.)

Core Features:
- Real-time calibration metrics
- Interactive visual components
- Historical performance trends
- Signal-specific breakdowns
- UX-focused design for ease of monitoring
"""

from flask import Flask, render_template_string, jsonify, request, redirect, url_for
import flask
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import sqlite3
import json
from datetime import datetime, timedelta
import os
import statistics
# Simulation data removed — Phase B: using real data only


# Configuration
PAPER_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/paper_trading.db"
METAR_DB_PATH = "/home/gaddams/.openclaw-next/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
STATIC_FOLDER = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/static"
TEMPLATES_FOLDER = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/templates"

# Create required directories
os.makedirs(STATIC_FOLDER, exist_ok=True)
os.makedirs(TEMPLATES_FOLDER, exist_ok=True)

# Initialize Flask App
app = Flask(__name__, static_folder=STATIC_FOLDER, template_folder=TEMPLATES_FOLDER)

def get_database_connection(db_path):
    """Get database connection with row factory."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn

def fetch_paper_trading_metrics():
    """Fetch key metrics from the paper trading database."""
    try:
        conn = get_database_connection(PAPER_DB_PATH)
        cursor = conn.cursor()
        
        # Overall performance metrics
        cursor.execute("""
            SELECT 
                date, 
                closing_balance,
                (closing_balance - opening_balance) as daily_pnl,
                winning_trades,
                losing_trades,
                trade_count,
                avg_confidence
            FROM daily_balances
            ORDER BY date DESC
            LIMIT 90
        """)
        
        performance_data = cursor.fetchall()
        
        # Calculate additional derived metrics
        overall_pnl = sum(row['daily_pnl'] for row in performance_data if row['daily_pnl'])
        win_rate = sum(row['winning_trades'] for row in performance_data) / sum(max(row['winning_trades'] + row['losing_trades'], 1) if row['winning_trades'] + row['losing_trades'] > 0 else 1 for row in performance_data)
        
        # Get trade version performance
        cursor.execute("""
            SELECT 
                trade_version,
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(realized_pnl) as total_pnl,
                AVG(confidence_indicator) as avg_confidence
            FROM trades 
            WHERE status = 'closed'
            GROUP BY trade_version
            ORDER BY total_pnl DESC
            LIMIT 10
        """)
        version_performance = cursor.fetchall()
        
        # Close connection
        conn.close()
        
        return {
            'performance_data': performance_data,
            'overall_pnl': overall_pnl,
            'win_rate': win_rate,
            'version_performance': version_performance
        }
    except Exception as e:
        print(f"ERROR: Cannot connect to paper trading database: {e}")
        return {
            'performance_data': [],
            'overall_pnl': 0.0,
            'win_rate': 0.0,
            'version_performance': []
        }

def create_portfolio_performance_plot(data):
    """Create portfolio performance plot."""
    if not data or len(data) == 0:
        return go.Figure().to_html(full_html=False)
        
    # Convert to dataframe
    df = pd.DataFrame([dict(row) for row in data])
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    fig = go.Figure()
    
    # Portfolio value over time
    fig.add_trace(go.Scatter(
        x=df['date'], 
        y=df['closing_balance'], 
        mode='lines', 
        name='Portfolio Balance',
        line=dict(width=2, color='#3498db'),
        hovertemplate='<b>%{x}</b><br>Bal: $%{y:,.2f}<extra></extra>'
    ))
    
    # Layout
    fig.update_layout(
        title={'text': 'Portfolio Performance Over Time', 'font': {'size': 16}},
        xaxis_title='Date',
        yaxis_title='Portfolio Value ($)',
        hovermode='x unified',
        font=dict(size=12),
        height=400,
        title_x=0,  # Left-align title
    )
    
    return fig.to_html(full_html=False, div_classes="chart-container")


def create_daily_pnl_plot(data):
    """Create daily P&L histogram/bar chart."""
    if not data or len(data) == 0:
        return go.Figure().to_html(full_html=False)
    
    # Convert to dataframe
    df = pd.DataFrame([dict(row) for row in data])
    
    fig = go.Figure()
    
    # Bar chart of daily pnl
    colors = df['daily_pnl'].apply(lambda x: '#e74c3c' if x < 0 else '#2ecc71')  # Red if loss, green if gain
    
    fig.add_trace(go.Bar(
        x=df['date'], 
        y=df['daily_pnl'],
        marker_color=colors,
        hovertemplate='<b>%{x}</b><br>Daily P&L: $%{y:,.2f}<extra></extra>',
        name='Daily P&L'
    ))
    
    fig.update_layout(
        title={'text': 'Daily Profit & Loss', 'font': {'size': 16}},
        xaxis_title='Date',
        yaxis_title='P&L ($)',
        font=dict(size=12),
        height=400,
        title_x=0,
    )
    
    return fig.to_html(full_html=False, div_classes="chart-container")


def create_confidence_calibration_plot(data):
    """Create confidence calibration plot from real calibration results."""
    # Try to load real calibration results
    try:
        cal_path = '/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/phaseB_calibration_results.json'
        if os.path.exists(cal_path):
            with open(cal_path, 'r') as f:
                cal_data = json.load(f)
            per_signal = cal_data.get('per_signal', {})
            if per_signal:
                # Build reliability diagram from per-signal accuracy
                accuracies = []
                confidences = []
                signal_names = []
                for sig, metrics in per_signal.items():
                    if metrics.get('total_trades', 0) > 50:
                        accuracies.append(metrics.get('calibrated_accuracy', 0.5))
                        confidences.append(metrics.get('calibrated_accuracy', 0.5))
                        signal_names.append(sig)

                fig = go.Figure()
                # Perfect calibration diagonal
                fig.add_trace(go.Scatter(
                    x=[0, 1], y=[0, 1],
                    mode='lines',
                    line=dict(color='red', dash='dash'),
                    name='Perfect Calibration'
                ))
                # Signal accuracy points
                fig.add_trace(go.Scatter(
                    x=confidences, y=accuracies,
                    mode='markers',
                    marker=dict(size=10, color='#3498db', opacity=0.7),
                    text=signal_names,
                    name='Signal Accuracy',
                    hovertemplate='<b>%{text}</b><br>Confidence: %{x:.2%}<br>Accuracy: %{y:.2%}<extra></extra>'
                ))
                fig.update_layout(
                    title={'text': 'Signal Calibration (from Phase B results)', 'font': {'size': 16}},
                    xaxis_title='Calibrated Confidence',
                    yaxis_title='Actual Accuracy',
                    height=500,
                    yaxis=dict(range=[0, 1]),
                    xaxis=dict(range=[0, 1]),
                )
                return fig.to_html(full_html=False, div_classes="chart-container")
    except Exception as e:
        print(f"Could not load calibration data: {e}")

    # Fallback: show empty state
    return go.Figure().update_layout(
        title="No calibration data available — run Phase B calibration first"
    ).to_html(full_html=False)


def create_performance_metrics_cards(metrics):
    """Create performance metrics for the dashboard."""
    return [
        {'title': 'Total P&L', 'value': f"${metrics['overall_pnl']:+,.2f}", 'color': 'blue'},
        {'title': 'Win Rate', 'value': f"{metrics['win_rate']*100:.2f}%", 'color': 'green'},
        {'title': 'Active Strategies', 'value': f"{len(metrics['version_performance'])}", 'color': 'purple'},
        {'title': 'Avg. Confidence', 'value': f"{np.mean([vp['avg_confidence'] or 0.5 for vp in metrics['version_performance']]):.2%}", 'color': 'orange'}
    ]


@app.route("/")
def dashboard():
    """Main dashboard page."""
    metrics = fetch_paper_trading_metrics()
    cards = create_performance_metrics_cards(metrics)
    portfolio_chart = create_portfolio_performance_plot(metrics['performance_data'])
    pnl_chart = create_daily_pnl_plot(metrics['performance_data'])
    calibration_chart = create_confidence_calibration_plot(metrics['performance_data'])
    
    # Top Performing Versions Table
    version_table = metrics['version_performance'][:5] if metrics['version_performance'] else []
    
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Weather Engine - Calibration Dashboard</title>
    <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <style>
        body {
            font-family: 'Segoe UI', sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f8fa;
            color: #2c3e50;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        header {
            background: white;
            border-radius: 8px;
            padding: 15px 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        h1 {
            margin: 0;
            color: #2c3e50;
            font-size: 28px;
        }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .metric-card {
            background: white;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        .metric-card:hover {
            transform: translateY(-5px);
        }
        .metric-title {
            font-size: 14px;
            color: #7f8c8d;
            margin-bottom: 8px;
        }
        .metric-value {
            font-size: 28px;
            font-weight: bold;
        }
        .blue { color: #3498db; }
        .green { color: #2ecc71; }
        .purple { color: #9b59b6; }
        .orange { color: #f39c12; }
        .charts {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(600px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .chart-container {
            background: white;
            border-radius: 8px;
            padding: 15px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        table {
            width: 100%;
            background: white;
            border-collapse: collapse;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ecf0f1;
        }
        th {
            background-color: #34495e;
            color: white;
        }
        tr:hover {
            background-color: #f9f9f9;
        }
        .table-container {
            margin-top: 20px;
        }
        footer {
            text-align: center;
            margin-top: 30px;
            color: #7f8c8d;
            font-size: 12px;
        }
        @media (max-width: 768px) {
            .charts {
                grid-template-columns: 1fr;
            }
            .metrics-grid {
                grid-template-columns: 1fr 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🌊 Weather Engine - Model Calibration Dashboard</h1>
            <p>Performance monitoring and real-time model calibration evaluation</p>
        </header>

        <div class="metrics-grid">
            <!-- Metrics will be inserted here -->
        </div>

        <div class="charts">
            <div class="chart-container">{{ portfolio_chart|safe }}</div>
            <div class="chart-container">{{ pnl_chart|safe }}</div>
        </div>

        <div class="chart-container">{{ calibration_chart|safe }}</div>

        <div class="table-container">
            <h3>Top Performing Strategy Versions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Version</th>
                        <th>Trades</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>P&L</th>
                        <th>Avg Confidence</th>
                    </tr>
                </thead>
                <tbody>
                    {% for row in version_table %}
                    <tr>
                        <td>{{ row['trade_version'] }}</td>
                        <td>{{ row['total_trades'] }}</td>
                        <td>{{ row['wins'] }}</td>
                        <td>{{ row['losses'] }}</td>
                        <td>${{ "%.2f"|format(row['total_pnl']) }}</td>
                        <td>{{ "%.1f"|format(row['avg_confidence']*100) }}%</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <footer>
            <p>Dashboard refreshed at {{ timestamp }} | Data from paper trading engine</p>
        </footer>
    </div>

    <script>
        // Insert metrics dynamically
        var metrics_data = {{ cards|tojson }};
        var container = $('.metrics-grid')[0];
        
        metrics_data.forEach(function(metric) {
            var card = document.createElement('div');
            card.className = 'metric-card';
            var colorClass = metric.color;
            
            card.innerHTML = '<div class="metric-title">' + metric.title + '</div>' +
                            '<div class="metric-value ' + colorClass + '">' + metric.value + '</div>';
                            
            container.appendChild(card);
        });
    </script>
</body>
</html>
    """
    
    # Render with dynamic values
    context = {
        'cards': cards,
        'profile_chart': portfolio_chart,
        'pnl_chart': pnl_chart,
        'calibration_chart': calibration_chart,
        'version_table': version_table,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')
    }
    
    return render_template_string(html, **context)


@app.route("/refresh")
def refresh_data():
    """Refresh and return the latest metrics."""
    try:
        metrics = fetch_paper_trading_metrics()
        return jsonify({
            'status': 'success',
            'overall_pnl': metrics['overall_pnl'],
            'win_rate': metrics['win_rate'],
            'version_performance_count': len(metrics['version_performance'])
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


def main():
    """Run the dashboard."""
    print("Starting Weather Engine Calibration Dashboard (v1.0)...")
    print("=" * 80)
    print("Dashboard URL: http://localhost:8086/")
    print("Press Ctrl+C to stop the server")
    print("=" * 80)
    
    os.makedirs(STATIC_FOLDER, exist_ok=True)
    os.makedirs(TEMPLATES_FOLDER, exist_ok=True)
    
    # Start the Flask app
    app.run(host='0.0.0.0', port=8086, debug=False, threaded=True)


if __name__ == '__main__':
    main()