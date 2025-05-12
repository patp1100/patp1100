"""
Enhanced Dashboard API for Options Wheel Strategy with 0DTE Support
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from flask import Flask, jsonify, make_response
from flask_cors import CORS

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Configuration with 0DTE parameters
CONFIG = {
    "MAX_POSITIONS": 5,
    "MAX_PORTFOLIO_RISK": 0.10,
    "UNDERLYING_SYMBOLS": [],
    "POSITION_STATE_FILE": "position_state_premium.json",
    "ZERO_DTE_ENABLED": True,
    "ZERO_DTE_MIN_PREMIUM": 0.01,
    "ZERO_DTE_MAX_RISK": 0.05,
    "ZERO_DTE_DAYS": [0, 1, 2, 3]  # Monday-Thursday
}


def setup_logging():
    """Configure application logging with rotation"""
    log_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # File handler with rotation
    file_handler = RotatingFileHandler(
        'dashboard_api.log',
        maxBytes=1024*1024,
        backupCount=3
    )
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.DEBUG)

    # Configure root logger
    app.logger.setLevel(logging.DEBUG)
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)


setup_logging()


def load_position_data() -> Dict[str, Any]:
    """Load position data from JSON file with error handling"""
    try:
        if not os.path.exists(CONFIG["POSITION_STATE_FILE"]):
            app.logger.warning(
                "Position state file not found, initializing empty state")
            return {
                "positions": {},
                "trade_history": []
            }

        with open(CONFIG["POSITION_STATE_FILE"], 'r', encoding='utf-8') as f:
            data = json.load(f)
            return {
                "positions": data.get("positions", {}),
                "trade_history": data.get("trade_history", [])
            }
    except (json.JSONDecodeError, OSError) as e:
        app.logger.error("Error loading position data: %s", str(e))
        return {
            "positions": {},
            "trade_history": []
        }


def calculate_portfolio_metrics(positions: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate portfolio-level metrics with 0DTE awareness
    Returns dictionary with portfolio statistics
    """
    total_premium = sum(
        float(pos.get("premiums", 0))
        for pos in positions.values()
    )

    active_positions = len(positions)
    zero_dte_positions = sum(
        1 for pos in positions.values() if pos.get("is_zero_dte", False)
    )

    # Calculate risk percentage
    risk_percentage = min(
        (active_positions / CONFIG["MAX_POSITIONS"]
         ) * CONFIG["MAX_PORTFOLIO_RISK"] * 100,
        CONFIG["MAX_PORTFOLIO_RISK"] * 100
    )

    # Determine next suggested action
    next_action = "Analyzing"
    if zero_dte_positions > 0:
        next_action = "Monitoring 0DTE positions"
    elif active_positions < CONFIG["MAX_POSITIONS"] / 2:
        next_action = "Look for new put opportunities"
    elif any(pos.get("state") == "ASSIGNED" for pos in positions.values()):
        next_action = "Consider selling calls on assigned positions"
    else:
        next_action = "Monitor existing positions"

    return {
        "total_premium": round(total_premium, 2),
        "active_positions": active_positions,
        "zero_dte_positions": zero_dte_positions,
        "portfolio_risk": round(risk_percentage, 1),
        "next_action": next_action,
        "timestamp": datetime.now().isoformat()
    }


@app.route('/api/dashboard', methods=['GET'])
def get_dashboard_data():
    """
    Main endpoint for dashboard data
    Returns JSON with positions, trade history, metrics and config
    """
    try:
        position_data = load_position_data()
        metrics = calculate_portfolio_metrics(position_data["positions"])

        # Sort trade history by timestamp (newest first) and limit to last 10
        sorted_history = sorted(
            position_data["trade_history"],
            key=lambda x: x.get("timestamp", ""),
            reverse=True
        )[:10]

        response_data = {
            "positions": position_data["positions"],
            "trade_history": sorted_history,
            "metrics": metrics,
            "config": {
                "max_positions": CONFIG["MAX_POSITIONS"],
                "max_risk": CONFIG["MAX_PORTFOLIO_RISK"] * 100,
                "underlying_symbols": CONFIG["UNDERLYING_SYMBOLS"],
                "zero_dte_enabled": CONFIG["ZERO_DTE_ENABLED"],
                "zero_dte_min_premium": CONFIG["ZERO_DTE_MIN_PREMIUM"],
                "zero_dte_max_risk": CONFIG["ZERO_DTE_MAX_RISK"] * 100,
                "zero_dte_days": CONFIG["ZERO_DTE_DAYS"]
            }
        }

        return jsonify(response_data)

    except (KeyError, ValueError, TypeError) as e:
        app.logger.error("Error generating dashboard data: %s", str(e))
        return make_response(
            jsonify({
                "error": "Internal server error",
                "details": str(e)
            }),
            500
        )


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
