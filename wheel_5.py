"""
# ------------------------------------------------------------------------------
# Name:        Automated Options Wheel Strategy
# Purpose:     Sell Puts and Calls automatically using Wheel Strategy
#
# Author:      Patrick Phillips
# Python:      3.11.9 64 bit
# Created:     11Mar2025
# Copyright:   (c) Patrick Phillips 4-April-2025
# License:     Private License
# This code may not be copied or distributed without permission from the
# author.
#
# DISCLAIMER:
# This code is for educational purposes only and should not be used for actual
# trading until the testing phase is complete.
# ----------------------------------------------------------------------------
# Pre-production code for the Options Wheel Strategy
# This code is provided "as is" without any warranty of any kind, either
# express or implied, including but not limited to the warranties of
# merchantability,
# -----------------------------------------------------------------------------
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
import json
from enum import Enum, auto
from typing import Optional, Dict, Tuple, List

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import ContractType
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest
from alpaca.trading.models import OptionContract

import config_wheel

# ----------------------- Configuration -----------------------
# Trading parameters
STRIKE_RANGE = 0.05
BUY_POWER_LIMIT = 0.10
RISK_FREE_RATE = 0.01
OI_THRESHOLD = 200
TARGET_STOP_LOSS_PERCENTAGE = 0.5
DELTA_STOP_LOSS_THRES = 2
SHORT_PUT_DELTA_RANGE = (-0.42, -0.18)
SHORT_CALL_DELTA_RANGE = (0.18, 0.42)

# Portfolio risk parameters
MAX_PORTFOLIO_RISK = 0.10
MAX_POSITIONS = 5
POSITION_SIZE = 0.05

# Stock symbols
underlying_symbols = [
    "MARA", "CHWY", "RIOT", "LMND", "KSS",
    "CLSK", "WOLF", "BAC", "INTC", "CELH",
    "JD", "TDOC", "SOFI", "AEO", "RBLX",
    "NIO", "DKNG", "CCL", "XLK", "UPST",
    "S", "XLF", "KO", "KR", "WBA",
    "F", "T", "FSLR"
]

# Initialize Alpaca clients
trade_client = TradingClient(
    config_wheel.APCA_API_KEY_ID,
    config_wheel.APCA_API_SECRET_KEY,
    paper=True
)
option_historical_data_client = OptionHistoricalDataClient(
    config_wheel.APCA_API_KEY_ID,
    config_wheel.APCA_API_SECRET_KEY
)
stock_data_client = StockHistoricalDataClient(
    config_wheel.APCA_API_KEY_ID,
    config_wheel.APCA_API_SECRET_KEY
)

# ----------------------- Trade State Enum -----------------------


class TradeState(Enum):
    """_summary_

    Args:
        Enum (_type_): _description_
    """
    PUT_SOLD = auto()
    ASSIGNED = auto()
    CALL_SOLD = auto()
    CALL_ASSIGNED = auto()

# ----------------------- Logging Setup -----------------------


def setup_logging() -> logging.Logger:
    """Configure file and console logging"""
    log_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler('options_wheel.log')
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.DEBUG)

    local_logger = logging.getLogger()
    local_logger.setLevel(logging.DEBUG)
    local_logger.addHandler(file_handler)
    local_logger.addHandler(console_handler)

    return local_logger


logger = setup_logging()

# ----------------------- Position Tracker -----------------------


class PositionTracker:
    """Track positions and cost basis"""

    def __init__(self):
        self.positions: Dict = {}
        self.trade_history: List = []
        self.load_state()

    def load_state(self) -> None:
        """Load saved state from file"""
        try:
            with open('position_state.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.positions = data.get('positions', {})
                self.trade_history = data.get('trade_history', [])
            logger.info("Loaded previous position state")
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("No previous position state found, starting fresh")
        except (OSError, IOError, ValueError) as e:
            logger.error("Error loading state: %s", str(e))

    def save_state(self) -> None:
        """Save current state to file"""
        try:
            with open('position_state.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'positions': self.positions,
                    'trade_history': self.trade_history
                }, f, indent=2)
        except (OSError, IOError, ValueError) as e:
            logger.error("Failed to save position state: %s", str(e))

    def update_position(
        self,
        stock_symbol: str,
        trade_price: float,
        qty: int,
        trade_type: TradeState,
        strike: Optional[float] = None
    ) -> None:
        """Update position tracking"""
        if stock_symbol not in self.positions:
            self.positions[stock_symbol] = {
                'shares': 0,
                'cost_basis': 0,
                'strike': None,
                'state': None,
                'premiums': 0
            }

        # Record trade in history
        self.trade_history.append({
            'timestamp': datetime.now(
                ZoneInfo("America/New_York")
            ).isoformat(),
            'symbol': stock_symbol,
            'type': trade_type.name,
            'price': trade_price,
            'quantity': qty,
            'strike': strike
        })

        pos = self.positions[stock_symbol]

        if trade_type == TradeState.PUT_SOLD:
            pos['premiums'] += trade_price * qty * 100
            pos['state'] = TradeState.PUT_SOLD
            if strike:
                pos['strike'] = strike

        elif trade_type == TradeState.ASSIGNED:
            if strike is None:
                logger.error("Strike price is None for ASSIGNED trade type")
                return
            total_cost = strike * qty * 100
            pos['shares'] += qty * 100
            pos['cost_basis'] = (
                (pos['cost_basis'] * (pos['shares'] - qty * 100)) + total_cost
            ) / pos['shares']
            pos['state'] = TradeState.ASSIGNED

        elif trade_type == TradeState.CALL_SOLD:
            pos['premiums'] += trade_price * qty * 100
            pos['state'] = TradeState.CALL_SOLD
            if strike:
                pos['strike'] = strike

        elif trade_type == TradeState.CALL_ASSIGNED:
            pos['shares'] -= qty * 100
            pos['state'] = None
            if pos['shares'] == 0:
                pnl = (strike - pos['cost_basis']) * \
                    qty * 100 + pos['premiums']
                logger.info(
                    "Wheel cycle completed for %s. P&L: $%.2f",
                    stock_symbol, pnl
                )
                del self.positions[stock_symbol]

        self.save_state()


# Initialize tracker
position_tracker = PositionTracker()

# ----------------------- Helper Functions -----------------------


def get_underlying_price(stock_symbol: str) -> float:
    """Fetch the current price of the underlying stock."""
    try:
        request = StockLatestBarRequest(symbol_or_symbols=stock_symbol)
        barset = stock_data_client.get_stock_latest_bar(request)
        if stock_symbol in barset and barset[stock_symbol].close is not None:
            return float(barset[stock_symbol].close)
        raise ValueError(f"Price for symbol {stock_symbol} is unavailable")
    except (KeyError, ValueError, AttributeError) as e:
        logger.error("Failed to fetch price for %s: %s", stock_symbol, str(e))
        raise


def fetch_put_options(
    stock_symbol: str,
    strike_threshold: str,
    expiration_start: datetime,
    max_exp_date: datetime
) -> List[OptionContract]:
    """Fetch available put options for a given stock symbol and criteria."""
    try:
        request = GetOptionContractsRequest(
            underlying_symbols=[stock_symbol],
            strike_price_gte=strike_threshold,
            expiration_date_gte=expiration_start.date(),
            expiration_date_lte=max_exp_date.date(),
            type=ContractType.PUT
        )
        response = trade_client.get_option_contracts(request)
        return [
            contract for contract in response
            if isinstance(contract, OptionContract)
        ] if response else []
    except (KeyError, ValueError, AttributeError) as e:
        logger.error(
            "Error fetching put options for %s: %s", stock_symbol, str(e)
        )
        return []


def check_portfolio_risk() -> Tuple[bool, str]:
    """Evaluate portfolio risk and return a tuple (risk_ok, risk_msg)."""
    try:
        account = trade_client.get_account()
        equity = float(getattr(account, 'equity', 0.0))
        buying_power = float(getattr(account, 'buying_power', 0.0))
        positions = len(position_tracker.positions)

        if positions >= MAX_POSITIONS:
            return False, "Maximum number of positions reached"
        if buying_power / equity < (1 - MAX_PORTFOLIO_RISK):
            return False, "Portfolio risk exceeds maximum allowed"

        return True, "Portfolio risk is within acceptable limits"
    except (KeyError, ValueError, AttributeError) as e:
        equity = 0.0
        logger.error("Error checking portfolio risk: %s", str(e))
        return False, "Error checking portfolio risk"


# ----------------------- Main Execution -----------------------
if __name__ == "__main__":
    logger.info("Starting Enhanced Options Wheel Strategy")
    timezone = ZoneInfo("America/New_York")

    while True:
        try:
            now = datetime.now(timezone)
            market_open_time = now.replace(
                hour=9, minute=30, second=0, microsecond=0)
            market_close_time = now.replace(
                hour=16, minute=0, second=0, microsecond=0)

            # Market hours check
            if now < market_open_time:
                sleep_seconds = (market_open_time - now).total_seconds()
                logger.info(
                    "Market not yet open. Sleeping for %.2f minutes",
                    sleep_seconds / 60
                )
                time.sleep(sleep_seconds)
                continue

            if now > market_close_time:
                next_market_open = market_open_time + timedelta(days=1)
                sleep_seconds = (next_market_open - now).total_seconds()
                logger.info(
                    "Market closed. Sleeping for %.2f hours",
                    sleep_seconds / 3600
                )
                time.sleep(sleep_seconds)
                continue

            # Portfolio risk check
            risk_ok, risk_msg = check_portfolio_risk()
            if not risk_ok:
                logger.warning("Skipping cycle due to risk: %s", risk_msg)
                time.sleep(60)
                continue

            # Main strategy execution
            current_date = now.date()
            min_expiration = datetime.combine(
                current_date + timedelta(days=5),
                datetime.min.time(),
                timezone)
            max_expiration = datetime.combine(
                current_date + timedelta(days=14),
                datetime.min.time(),
                timezone
            )

            # Filter candidates
            candidate_stocks = []
            for symbol in underlying_symbols:
                try:
                    if symbol in position_tracker.positions:
                        continue

                    price = get_underlying_price(symbol)
                    MIN_STRIKE = str(price * (1 - STRIKE_RANGE))

                    put_options = fetch_put_options(
                        symbol,
                        MIN_STRIKE,
                        min_expiration,
                        max_expiration
                    )

                    if put_options:
                        candidate_stocks.append((symbol, put_options[0]))
                except (KeyError, ValueError, AttributeError) as e:
                    logger.error("Error evaluating %s: %s", symbol, str(e))
                    continue

            # Execute strategy for candidates (simplified)
            for symbol, put_option in candidate_stocks[:3]:
                try:
                    logger.info(
                        "Would execute trade for %s at strike %s",
                        symbol, put_option.strike_price
                    )
                    # Actual execution would go here
                except (KeyError, ValueError, AttributeError) as e:
                    logger.error(
                        "Error executing strategy for %s: %s", symbol, str(e))

            time.sleep(60)

        except (KeyError, ValueError, AttributeError, RuntimeError) as e:
            logger.error("Unexpected error in main loop: %s", str(e))
            time.sleep(300)
