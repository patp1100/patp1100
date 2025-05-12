"""
# ------------------------------------------------------------------------------
# Name:        Automated Options Wheel Strategy (Premium-Optimized)
# Purpose:     Sell Puts and Calls automatically using Wheel Strategy
#              with focus on maximizing premium collection
#
# Author:      Patrick Phillips
# Python:      3.11.9 64 bit
# Created:     29-Mar-2025
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
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import time
import json
from enum import Enum, auto
from typing import Optional, Dict, Tuple, List, Any
import os
import numpy as np
import backoff

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetAssetsRequest
)
from alpaca.trading.enums import (
    ContractType,
    AssetStatus,
    AssetClass)
from alpaca.data.historical.option import OptionHistoricalDataClient

from alpaca.data.requests import (
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockBarsRequest,
    OptionLatestQuoteRequest
)
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.models import OptionContract

import config_wheel

# ----------------------- Configuration -----------------------
# Trading parameters
STRIKE_RANGE: float = 0.05  # Base percentage from current price
# to consider strikes
BUY_POWER_LIMIT: float = 0.10
RISK_FREE_RATE: float = 0.01
OI_THRESHOLD: int = 200
TARGET_STOP_LOSS_PERCENTAGE: float = 0.5
DELTA_STOP_LOSS_THRES: float = 2
# Optimal delta range for premium
SHORT_PUT_DELTA_RANGE: Tuple[float, float] = (-0.42, -0.18)
SHORT_CALL_DELTA_RANGE: Tuple[float, float] = (
    0.18, 0.42)  # Optimal delta range for premium

# Portfolio risk parameters
MAX_PORTFOLIO_RISK: float = 0.10
MAX_POSITIONS: int = 5
POSITION_SIZE: float = 0.05  # Percentage of portfolio per position

# Premium optimization parameters
MIN_PREMIUM_RETURN: float = 0.03  # Minimum 3% return on capital at risk
DAYS_TO_EXPIRATION_RANGE: Tuple[int, int] = (
    5, 14)  # Base DTE range for premium selling

# Stock selection parameters
MIN_STOCK_PRICE: float = 10
MAX_STOCK_PRICE: float = 50
TARGET_STOCK_COUNT: int = 100
MIN_AVERAGE_VOLUME: int = 500000  # Minimum average daily volume

# Initialize Alpaca clients
trade_client = TradingClient(
    config_wheel.APCA_API_KEY_ID, config_wheel.APCA_API_SECRET_KEY, paper=True
)
option_historical_data_client = OptionHistoricalDataClient(
    config_wheel.APCA_API_KEY_ID, config_wheel.APCA_API_SECRET_KEY
)
stock_data_client = StockHistoricalDataClient(
    config_wheel.APCA_API_KEY_ID, config_wheel.APCA_API_SECRET_KEY
)

# ----------------------- Logging Setup -----------------------


def setup_logging() -> logging.Logger:
    """Configure file and console logging with more detailed format"""
    log_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Reduce noise from underlying libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("alpaca").setLevel(logging.INFO)

    file_handler = logging.FileHandler("options_wheel_premium.log")
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

# ----------------------- Trade State Enum -----------------------


class TradeState(Enum):
    """Enum to track the current state of each position
    in the wheel strategy"""
    PUT_SOLD = auto()
    ASSIGNED = auto()
    CALL_SOLD = auto()
    CALL_ASSIGNED = auto()

# ----------------------- Position Tracker -----------------------


class PositionTracker:
    """Enhanced position tracker with premium optimization metrics"""

    def __init__(self):
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_history: List[Dict[str, Any]] = []
        self.load_state()

    def load_state(self) -> None:
        """Load saved state from file with error handling"""
        try:
            with open(
                "position_state_premium.json", "r", encoding="utf-8"
            ) as f:
                data = json.load(f)
                # Handle both old (list) and new (dict) format
                if isinstance(data, list):
                    # Old format - list of trades
                    self.positions = {}
                    self.trade_history = data
                    logger.warning(
                        "Legacy position state format detected - converting")
                else:
                    # New format - dictionary with positions and history
                    self.positions = data.get("positions", {})
                    self.trade_history = data.get("trade_history", [])
            logger.info("Loaded previous position state")
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in position state file, starting fresh")
            self.positions = {}
            self.trade_history = []
        except FileNotFoundError:
            logger.warning("No previous position state found, starting fresh")
            self.positions = {}
            self.trade_history = []
        except OSError as e:
            logger.error("Error loading state: %s", str(e))
            self.positions = {}
            self.trade_history = []

    def save_state(self) -> None:
        """Save current state to file with atomic write operation"""
        try:
            temp_file = "position_state_premium.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"positions": self.positions,
                        "trade_history": self.trade_history},
                    f,
                    indent=2,
                )
            # Atomic rename
            os.replace(temp_file, "position_state_premium.json")
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            logger.error("Failed to save position state: %s", str(e))

    def update_position(
        self,
        stock_symbol: str,
        trade_price: float,
        quantity: int,
        trade_type: TradeState,
        strike: Optional[float] = None,
        premium: Optional[float] = None,
    ) -> None:
        """Update position tracking with premium metrics"""
        if stock_symbol not in self.positions:
            self.positions[stock_symbol] = {
                "shares": 0,
                "cost_basis": 0.0,
                "strike": None,
                "state": None,
                "premiums": 0.0,
                "premium_percentage": 0.0,
                "dte_at_open": 0,
                "trade_dates": [],
            }

        # Record trade in history with more details
        trade_record = {
            "timestamp": datetime.now(
                ZoneInfo("America/New_York")
            ).isoformat(),
            "symbol": stock_symbol,
            "type": trade_type.name,
            "price": trade_price,
            "quantity": quantity,
            "strike": strike,
            "premium": premium,
            "premium_pct": (premium / strike) * 100 if strike
            and premium else None,
        }
        self.trade_history.append(trade_record)
        self.positions[stock_symbol]["trade_dates"].append(
            trade_record["timestamp"])

        pos = self.positions[stock_symbol]

        if trade_type == TradeState.PUT_SOLD:
            if strike is None or premium is None:
                logger.error(
                    "Strike and premium must be provided for PUT_SOLD")
                return
            premium_amount = premium * quantity * 100
            pos["premiums"] += premium_amount
            pos["strike"] = strike
            pos["premium_percentage"] = (premium / strike) * 100
            try:
                trade_date = datetime.strptime(
                    trade_record["timestamp"][:10], "%Y-%m-%d")
                expiration_date = datetime.strptime(
                    trade_record["timestamp"][:10], "%Y-%m-%d") + timedelta(
                        days=DAYS_TO_EXPIRATION_RANGE[1]
                )
                pos["dte_at_open"] = (expiration_date - trade_date).days
            except (ValueError, TypeError) as e:
                logger.error("Error calculating DTE: %s", str(e))
                pos["dte_at_open"] = DAYS_TO_EXPIRATION_RANGE[1]
            pos["state"] = TradeState.PUT_SOLD

        elif trade_type == TradeState.ASSIGNED:
            if strike is None:
                logger.error("Strike price is None for ASSIGNED trade type")
                return
            total_cost = strike * quantity * 100
            if pos["shares"] == 0:
                pos["cost_basis"] = strike
            else:
                pos["cost_basis"] = (
                    (pos["cost_basis"] * pos["shares"] + total_cost) /
                    (pos["shares"] + quantity * 100)
                )
            pos["shares"] += quantity * 100
            pos["state"] = TradeState.ASSIGNED

        elif trade_type == TradeState.CALL_SOLD:
            if strike is None or premium is None:
                logger.error(
                    "Strike and premium must be provided for CALL_SOLD")
                return
            premium_amount = premium * quantity * 100
            pos["premiums"] += premium_amount
            pos["strike"] = strike
            pos["premium_percentage"] = (premium / strike) * 100
            try:
                trade_date = datetime.strptime(
                    trade_record["timestamp"][:10], "%Y-%m-%d")
                expiration_date = datetime.strptime(
                    trade_record["timestamp"][:10], "%Y-%m-%d") + timedelta(
                        days=DAYS_TO_EXPIRATION_RANGE[1]
                )
                pos["dte_at_open"] = (expiration_date - trade_date).days
            except (ValueError, TypeError) as e:
                logger.error("Error calculating DTE: %s", str(e))
                pos["dte_at_open"] = DAYS_TO_EXPIRATION_RANGE[1]
            pos["state"] = TradeState.CALL_SOLD

        elif trade_type == TradeState.CALL_ASSIGNED:
            if strike is None:
                logger.error(
                    "Strike price is None for CALL_ASSIGNED trade type")
                return
            pos["shares"] -= quantity * 100
            pos["state"] = None
            if pos["shares"] == 0:
                pnl = (strike - pos["cost_basis"]) * \
                    quantity * 100 + pos["premiums"]
                logger.info(
                    "Wheel cycle completed for %s. P&L: $%.2f (%.2f%%)",
                    stock_symbol,
                    pnl,
                    (pnl / (strike * quantity * 100)) *
                    100 if strike is not None else 0,
                )
                del self.positions[stock_symbol]

        self.save_state()


position_tracker = PositionTracker()

# ---------------- API Helpers with Throttling Protection ------------------


@backoff.on_exception(backoff.expo, Exception, max_tries=3, logger=logger)
def safe_api_call(api_func, *args, **kwargs):
    """Wrapper with retry logic for API calls"""
    return api_func(*args, **kwargs)

# ---------------- Stock Selection Functions ------------------


def get_tradable_stocks() -> List[str]:
    """Fetch active, tradable stocks between $10 and $50
    with sufficient volume"""
    try:
        # Get all active stocks
        request = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE
        )
        assets = safe_api_call(trade_client.get_all_assets, request)

        # Filter stocks with options and in our price range
        filtered_symbols = []
        for asset in assets:
            if asset.symbol and asset.tradable and asset.fractionable:
                filtered_symbols.append(asset.symbol)

        # Process in batches to avoid hitting API limits
        batch_size = 100
        selected_stocks = []

        for i in range(0, len(filtered_symbols), batch_size):
            batch = filtered_symbols[i:i+batch_size]
            price_data = {}

            try:
                # Try using IEX feed first
                feeds_to_try = [DataFeed.IEX, DataFeed.SIP]
                for feed in feeds_to_try:
                    try:
                        quote_request = StockLatestQuoteRequest(
                            symbol_or_symbols=batch,
                            feed=feed
                        )
                        quote_data = safe_api_call(
                            stock_data_client.get_stock_latest_quote,
                            quote_request
                        )

                        for batch_symbol in batch:
                            if (batch_symbol in quote_data and
                                    quote_data[batch_symbol].ask_price
                                    is not None):
                                price_data[batch_symbol] = float(
                                    quote_data[batch_symbol].ask_price)
                        break  # Successfully got data from this feed
                    except (ValueError, KeyError, RuntimeError):
                        continue

                # Fallback to trades if quotes not available
                missing_symbols = [s for s in batch if s not in price_data]
                if missing_symbols:
                    for feed in feeds_to_try:
                        try:
                            trade_request = StockLatestTradeRequest(
                                symbol_or_symbols=missing_symbols,
                                feed=feed
                            )
                            trade_data = safe_api_call(
                                stock_data_client.get_stock_latest_trade,
                                trade_request
                            )

                            for batch_symbol in missing_symbols:
                                if (batch_symbol in trade_data and
                                        trade_data[batch_symbol].price
                                        is not None):
                                    price_data[batch_symbol] = float(
                                        trade_data[batch_symbol].price)
                            break
                        except (ValueError, KeyError, RuntimeError):
                            continue

            except (ValueError, KeyError, RuntimeError) as e:
                logger.warning(
                    "Error getting prices for batch starting at %d: %s",
                    i, str(e))
                continue

            # Get volume data for symbols with valid prices
            volume_data = {}
            if price_data:
                try:
                    volume_request = StockBarsRequest(
                        symbol_or_symbols=list(price_data.keys()),
                        timeframe=TimeFrame.Day,  # type: ignore
                        start=datetime.now(
                            ZoneInfo("America/New_York")) - timedelta(days=5),
                        end=datetime.now(ZoneInfo("America/New_York")),
                        feed=DataFeed.IEX
                    )
                    volume_data = safe_api_call(
                        stock_data_client.get_stock_bars, volume_request)
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.warning(
                        "Error getting volume data for batch "
                        "starting at %d: %s", i, str(e)
                    )

            # Process valid stocks in this batch
            for batch_symbol, price in price_data.items():
                try:
                    # Price range check
                    if not (
                        MIN_STOCK_PRICE <= price <= MAX_STOCK_PRICE
                    ):
                        continue

                    # Volume check if data is available
                    if batch_symbol in volume_data and len(
                        volume_data[batch_symbol]
                    ) > 0:
                        try:
                            avg_volume = np.mean(
                                [
                                    daily_bar.volume
                                    for daily_bar in volume_data[batch_symbol]
                                ]
                            )
                            if avg_volume < MIN_AVERAGE_VOLUME:
                                continue
                        except (ValueError, KeyError, RuntimeError) as e:
                            logger.debug(
                                "Couldn't calculate volume for %s: %s",
                                batch_symbol, str(e))
                            # Include anyway if we can't verify volume

                    selected_stocks.append(batch_symbol)

                    if len(selected_stocks) >= TARGET_STOCK_COUNT:
                        break

                except (ValueError, KeyError, RuntimeError) as e:
                    logger.debug("Error processing %s: %s",
                                 batch_symbol, str(e))
                    continue

            if len(selected_stocks) >= TARGET_STOCK_COUNT:
                break

        logger.info("Selected %d stocks for options wheel",
                    len(selected_stocks))
        return selected_stocks

    except (ValueError, KeyError, RuntimeError) as e:
        logger.error("Error fetching tradable stocks: %s", str(e))
        # Fallback to predefined list
        return [
            "AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "PYPL",
            "ADBE", "NFLX", "INTC", "CSCO", "CMCSA", "PEP", "AVGO", "TXN",
            "QCOM", "TMUS", "AMGN", "COST", "SBUX", "MDLZ", "BKNG", "INTU",
            "AMD", "GILD", "FISV", "VRTX", "ISRG", "REGN", "ADP", "ATVI",
            "MU", "ADI", "CSX", "MELI", "CHTR", "MAR", "KLAC", "PANW",
            "SNPS", "CDNS", "ASML", "ORLY", "MNST", "LRCX", "NXPI", "KDP",
            "EXC", "AEP", "WDAY", "IDXX", "MCHP", "DXCM", "BIIB", "EA",
            "CTSH", "VRSK", "ILMN", "XEL", "DLTR", "ROST", "ODFL", "PCAR",
            "PAYX", "ANSS", "CPRT", "FAST", "SIRI", "SWKS", "TTWO", "VRSN",
            "WBA", "WBD", "ZM", "DOCU", "DASH", "RIVN", "LCID", "PTON",
            "HOOD", "COIN", "SNAP", "TWLO", "SQ", "CRWD", "ZS", "MDB", "NET",
            "OKTA", "DDOG", "TEAM", "PLTR", "ASAN", "U", "PATH", "BILL",
            "SNOW", "ESTC", "AFRM"
        ]

# ---------------- Dynamic Strike and Expiration Functions ------------------


def get_dynamic_strike_range(current_price: float) -> Tuple[float, float]:
    """Adjust strike range based on volatility"""
    base_range = STRIKE_RANGE
    if current_price < 20:
        base_range *= 1.3
    lower_bound = current_price * (1 - base_range)
    upper_bound = current_price * (1 + base_range * 0.5)
    return (lower_bound, upper_bound)


def get_expiration_window() -> Tuple[date, date]:
    """Smart expiration window based on market conditions"""
    base_days = DAYS_TO_EXPIRATION_RANGE
    current_date = datetime.now(ZoneInfo("America/New_York")).date()

    if len(position_tracker.positions) < MAX_POSITIONS / 2:
        base_days = (base_days[0], min(base_days[1] + 7, 21))

    return (
        current_date + timedelta(days=base_days[0]),
        current_date + timedelta(days=base_days[1]),
    )

# ----------------------- Premium Optimization Functions -------------------


def calculate_premium_metrics(
    option: OptionContract, underlying_price: float
) -> Dict[str, float]:
    """Calculate premium metrics for a given option"""
    try:
        put_quote_request = OptionLatestQuoteRequest(
            symbol_or_symbols=[option.symbol])
        quote = safe_api_call(
            option_historical_data_client.get_option_latest_quote,
            put_quote_request
        )

        if not quote or option.symbol not in quote:
            return {}

        latest_quote = quote[option.symbol]
        option_bid_price = float(latest_quote.bid_price)
        option_ask_price = float(latest_quote.ask_price)
        calculated_mid_price = (option_bid_price + option_ask_price) / 2
        option_strike_price = float(option.strike_price)

        return {
            "bid": option_bid_price,
            "ask": option_ask_price,
            "mid": calculated_mid_price,
            "premium_pct": (calculated_mid_price / option_strike_price) * 100,
            "bid_ask_spread": option_ask_price - option_bid_price,
            "spread_pct": (
                (
                    (option_ask_price - option_bid_price)
                    / calculated_mid_price
                    * 100
                )
            ),
            "strike": option_strike_price,
            "moneyness": (
                (option_strike_price - underlying_price) / underlying_price
            ),
        }
    except (ValueError, KeyError, RuntimeError) as e:
        logger.error("Error calculating premium metrics: %s", str(e))
        return {}


def select_best_premium_option(
    option_list: List[OptionContract], underlying_price: float
) -> Optional[OptionContract]:
    """Select the option with the best premium characteristics"""
    if not option_list:
        return None

    scored_options = []
    for option in option_list:
        metrics = calculate_premium_metrics(option, underlying_price)
        if not metrics:
            continue

        # Score based on premium percentage and spread
        spread_penalty = min(metrics["spread_pct"]
                             * 2, 10)  # Cap penalty at 10
        score = metrics["premium_pct"] - spread_penalty

        # Add small randomization to avoid always picking the same one
        score += np.random.uniform(-0.1, 0.1)

        scored_options.append((score, option, metrics))

    if not scored_options:
        return None

    # Sort by score descending
    scored_options.sort(key=lambda x: x[0], reverse=True)

    # Return the top scoring option that meets minimum criteria
    for score, option, metrics in scored_options:
        if (metrics["premium_pct"] >= MIN_PREMIUM_RETURN * 100 and
                metrics["spread_pct"] < 20):
            logger.debug(
                (
                    "Selected option: %s, Score: %.2f, "
                    "Premium: %.2f%%, Spread: %.2f%%"
                ),
                option.symbol,
                score,
                metrics["premium_pct"],
                metrics["spread_pct"],
            )
            return option

    return None

# ----------------------- Enhanced Helper Functions -----------------------


def fetch_put_options(
    stock_symbol: str,
    min_strike_price: float,
    min_expiration: datetime,
    max_expiration: date,
) -> List[OptionContract]:
    """Fetch available put options with better error handling"""
    try:
        request = GetOptionContractsRequest(
            underlying_symbols=[stock_symbol],
            strike_price_gte=str(min_strike_price),
            expiration_date_gte=min_expiration,
            expiration_date_lte=max_expiration,
            type=ContractType.PUT,
        )
        response = safe_api_call(trade_client.get_option_contracts, request)

        if not response:
            logger.warning(
                "No put options found for %s with given criteria", stock_symbol
            )
            return []

        return [
            contract
            for contract in response
            if isinstance(contract, OptionContract)
        ]
    except (ValueError, KeyError, RuntimeError) as e:
        logger.error("Error fetching put options for %s: %s",
                     stock_symbol, str(e))
        return []


def get_underlying_price(stock_symbol: str) -> float:
    """Fetch the current price of the underlying stock with retry logic"""
    max_retries = 3
    feeds_to_try = [DataFeed.IEX, DataFeed.SIP]

    for attempt in range(max_retries):
        try:
            # Try different data feeds
            for feed in feeds_to_try:
                try:
                    # First try using trade data
                    trade_request = StockLatestTradeRequest(
                        symbol_or_symbols=stock_symbol,
                        feed=feed
                    )
                    trade = safe_api_call(
                        stock_data_client.get_stock_latest_trade,
                        trade_request
                    )

                    if stock_symbol in trade and \
                            trade[stock_symbol].price is not None:
                        return float(trade[stock_symbol].price)

                    # Fallback to quote if trade isn't available
                    quote_request = StockLatestQuoteRequest(
                        symbol_or_symbols=stock_symbol,
                        feed=feed
                    )
                    quote = safe_api_call(
                        stock_data_client.get_stock_latest_quote,
                        quote_request
                    )

                    if (stock_symbol in quote and
                            quote[stock_symbol].ask_price is not None):
                        return float(quote[stock_symbol].ask_price)
                except (ValueError, KeyError, RuntimeError):
                    continue

            raise ValueError(f"Price for symbol {stock_symbol} is unavailable")

        except (ValueError, KeyError, RuntimeError) as e:
            if attempt == max_retries - 1:
                logger.error(
                    "Failed to fetch price for %s after %d attempts: %s",
                    stock_symbol,
                    max_retries,
                    str(e),
                )
                return get_recent_close_price(stock_symbol)
            time.sleep(1)

    return (MIN_STOCK_PRICE + MAX_STOCK_PRICE) / 2  # Ultimate fallback


def get_recent_close_price(stock_symbol: str) -> float:
    """Fallback method to get recent closing price"""
    try:
        request = StockBarsRequest(
            symbol_or_symbols=stock_symbol,
            timeframe=TimeFrame.Day,  # type: ignore
            start=datetime.now(ZoneInfo("America/New_York")
                               ) - timedelta(days=5),
            end=datetime.now(ZoneInfo("America/New_York")),
            feed=DataFeed.IEX
        )
        bars = safe_api_call(stock_data_client.get_stock_bars, request)
        if stock_symbol in bars and len(bars[stock_symbol]) > 0:
            return float(bars[stock_symbol][-1].close)
    except (ValueError, KeyError, RuntimeError) as e:
        logger.warning(
            "Failed to get recent close for %s: %s", stock_symbol, str(e)
        )
    return (MIN_STOCK_PRICE + MAX_STOCK_PRICE) / 2

# ----------------------- Portfolio Risk Check -----------------------


def check_portfolio_risk() -> Tuple[bool, str]:
    """Enhanced portfolio risk check with premium collection considerations"""
    try:
        account = safe_api_call(trade_client.get_account)
        buying_power = float(getattr(account, "buying_power", 0.0))
        account_equity = float(getattr(account, "equity", 0.0))
        positions = len(position_tracker.positions)

        if account_equity <= 0:
            return False, "Account equity is zero or negative"

        # Calculate total premium collected
        total_premium = sum(
            pos["premiums"] for pos in position_tracker.positions.values()
        )

        if positions >= MAX_POSITIONS:
            return False, (
                f"Max positions reached ({positions}/{MAX_POSITIONS})"
            )

        risk_ratio = (account_equity - buying_power) / account_equity
        if risk_ratio > MAX_PORTFOLIO_RISK:
            risk_percentage = MAX_PORTFOLIO_RISK * 100
            return False, f"Risk exceeds {risk_percentage:.1f}% of equity"

        logger.info(
            "Portfolio status: Positions %d/%d, Risk %.1f%%, Premiums $%.2f",
            positions,
            MAX_POSITIONS,
            ((account_equity - buying_power) / account_equity) * 100,
            total_premium,
        )
        return True, "Portfolio risk is within acceptable limits"
    except (ValueError, KeyError, RuntimeError) as e:
        logger.error("Error checking portfolio risk: %s", str(e))
        return False, "Error checking portfolio risk"

# ----------------------- Main Execution -----------------------


if __name__ == "__main__":
    logger.info("Starting Premium-Optimized Options Wheel Strategy")
    timezone = ZoneInfo("America/New_York")

    # Get tradable stocks
    underlying_symbols = get_tradable_stocks()

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
                    sleep_seconds / 60,
                )
                time.sleep(sleep_seconds)
                continue

            if now > market_close_time:
                next_market_open = market_open_time + timedelta(days=1)
                sleep_seconds = (next_market_open - now).total_seconds()
                logger.info(
                    "Market closed. Sleeping for %.2f hours",
                    sleep_seconds / 3600,
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
            candidate_stocks = []
            for symbol in underlying_symbols:
                try:
                    if symbol in position_tracker.positions:
                        continue

                    stock_price = get_underlying_price(symbol)
                    min_strike, max_strike = get_dynamic_strike_range(
                        stock_price)
                    min_exp, max_exp = get_expiration_window()

                    options = fetch_put_options(
                        stock_symbol=symbol,
                        min_strike_price=min_strike,
                        min_expiration=datetime.combine(
                            min_exp,
                            datetime.min.time(),
                            ZoneInfo("America/New_York")
                        ),
                        max_expiration=max_exp,
                    )

                    if not options:
                        logger.debug(
                            (
                                "No valid puts found for %s "
                                "(strike $%.2f-$%.2f, exp %s-%s), skipping"
                            ),
                            symbol,
                            min_strike,
                            max_strike,
                            min_exp,
                            max_exp,
                        )
                        continue

                    best_option = select_best_premium_option(
                        options, stock_price)
                    if best_option:
                        candidate_stocks.append((symbol, best_option))
                except (ValueError, KeyError, RuntimeError) as e:
                    logger.error("Failed to process %s: %s", symbol, str(e))
                    continue

            # Execute strategy for top candidates based on premium
            max_candidates = MAX_POSITIONS - len(position_tracker.positions)
            for symbol, put_option in candidate_stocks[:max_candidates]:
                try:
                    # Calculate position size
                    current_account = safe_api_call(trade_client.get_account)
                    current_account_equity = float(
                        getattr(current_account, "equity", 0.0))
                    max_position_size = current_account_equity * POSITION_SIZE
                    strike_price = float(put_option.strike_price)
                    qty = int(max_position_size // (strike_price * 100))

                    if qty < 1:
                        logger.warning(
                            (
                                "Insufficient capital for %s "
                                "(Need $%.2f, Have $%.2f)"
                            ),
                            symbol,
                            strike_price * 100,
                            max_position_size,
                        )
                        continue

                    # Get latest quote for execution
                    inner_put_quote_request = OptionLatestQuoteRequest(
                        symbol_or_symbols=[put_option.symbol]
                    )
                    inner_quote = safe_api_call(
                        option_historical_data_client.get_option_latest_quote,
                        inner_put_quote_request,
                    )

                    if not inner_quote or put_option.symbol not in inner_quote:
                        logger.error("No quote available for %s",
                                     put_option.symbol)
                        continue

                    # Use mid price for simulation
                    bid_price = float(inner_quote[put_option.symbol].bid_price)
                    ask_price = float(inner_quote[put_option.symbol].ask_price)
                    mid_price = (bid_price + ask_price) / 2

                    logger.info(
                        (
                            "Executing PUT SELL for %s: %d contracts at $%.2f "
                            "(strike $%.2f, %.2f%% return)"
                        ),
                        symbol,
                        qty,
                        mid_price,
                        strike_price,
                        (mid_price / strike_price) * 100,
                    )

                    # Update position tracker (simulated execution)
                    position_tracker.update_position(
                        stock_symbol=symbol,
                        trade_price=mid_price,
                        quantity=qty,
                        trade_type=TradeState.PUT_SOLD,
                        strike=strike_price,
                        premium=mid_price,
                    )

                except (ValueError, KeyError, RuntimeError) as e:
                    logger.error(
                        "Error executing strategy for %s: %s", symbol, str(e))

            time.sleep(60)

        except (ValueError, KeyError, RuntimeError) as e:
            logger.error("Unexpected error in main loop: %s", str(e))
            time.sleep(300)
