"""
Trading Dashboard — Phase 17

Production trading dashboard package for the weather trading engine.
Deprecates old dashboards (dashboard.py, confidence_dashboard.py, calibration_dashboard.py).

Blueprint-based Flask app with:
- P&L + Positions
- Alert Feed + Position Management
- Risk Dashboard
- Performance Analytics
- SSE endpoint for real-time updates

Usage:
    from core.trading_dashboard.routes import trading_bp
    app.register_blueprint(trading_bp, url_prefix="/trading")
"""

__version__ = "17.0.0"
__all__ = ["trading_bp"]