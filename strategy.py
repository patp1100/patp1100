"""

# ------------------------------------------------------------------------
# Name:        APremium-Optimized Options Wheel Strategy with 0DTE Support
# Purpose:     Sell Puts and Calls automatically using Wheel Strategy
#              with focus on maximizing premium collection and 0DTE support
#
# Author:      Patrick Phillips
# Python:      3.11.9 64 bit
# Created:     8-May-2025
# Copyright:   (c) Patrick Phillips 2025
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
Complete implementation with:
- Robust configuration handling
- Proper error recovery
- Full position tracking
- Dynamic adjustments
"""

import hashlib
import logging
import threading
import time
from datetime import date, datetime, timedelta
from enum import Enum, auto
from functools import lru_cache
from typing import Dict, List, Optional
from dataclasses import dataclass
import backoff
from config import get_config

# Initialize configuration
config = get_config()

# Initialize logger
logger = logging.getLogger(__name__)

# ----------------------- Custom Exceptions -----------------------


class TradingStrategyError(Exception):
    """Base exception for trading strategy errors."""


class CircuitBreakerError(TradingStrategyError):
    """Exception raised when circuit breaker is triggered."""


class InsufficientLiquidityError(TradingStrategyError):
    """Exception raised when liquidity requirements aren't met."""


class RiskLimitExceededError(TradingStrategyError):
    """Exception raised when risk limits are exceeded."""

# ----------------------- Core Enums -----------------------


class TradeState(Enum):
    """Represents the current state of a trade."""
    PUT_SOLD = auto()
    CALL_SOLD = auto()
    PUT_BOUGHT = auto()
    CALL_BOUGHT = auto()


class ContractType(Enum):
    """Type of options contract."""
    PUT = auto()
    CALL = auto()


class DataFeed(Enum):
    """Data feed source for market data."""
    IEX = auto()
    SIP = auto()

# ----------------------- Data Structures -----------------------


@dataclass
class StockLatestQuoteRequest:
    """Request object for getting stock quotes."""
    symbol_or_symbols: List[str]
    feed: DataFeed


@dataclass
class OptionContract:
    """Represents an options contract."""
    type: ContractType
    expiration_date: date
    strike_price: float
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    delta: float = 0.0
    volume: int = 0
    open_interest: int = 0


@dataclass
class Stock:
    """Represents a stock for potential trading."""
    symbol: str
    price: float
    avg_volume: float
    volatility: float
    last_earnings: date

# ----------------------- Core Components -----------------------


class OptionCache:
    """Advanced option price cache with volatility-based invalidation."""

    def __init__(self, max_size=1000, ttl=300):
        self.cache = {}
        self.max_size = max_size
        self.ttl = ttl  # seconds
        self.hits = 0
        self.misses = 0

    def get(
        self, symbol: str, option_type: str, expiration: str, strike: float
    ) -> Optional[Dict]:
        """Get cached option data if valid."""
        key = self._get_key(symbol, option_type, expiration, strike)
        entry = self.cache.get(key)

        if entry and (datetime.now() - entry['timestamp']).seconds < self.ttl:
            self.hits += 1
            return entry['data']

        self.misses += 1
        return None

    def set(
        self,
        symbol: str,
        option_type: str,
        expiration: str,
        strike: float,
        data: Dict
    ):
        """Set cache entry."""
        if len(self.cache) >= self.max_size:
            self.cache.pop(next(iter(self.cache)))

        key = self._get_key(symbol, option_type, expiration, strike)
        self.cache[key] = {'timestamp': datetime.now(), 'data': data}

    def _get_key(
        self, symbol: str, option_type: str, expiration: str, strike: float
    ) -> str:
        """Generate cache key using MD5 hash."""
        key_data = f"{symbol}{option_type}{expiration}{strike}"
        return hashlib.md5(key_data.encode()).hexdigest()


class PositionTracker:
    """Tracks all positions with tax lot accounting."""

    def log_metrics(self):
        """Log metrics related to positions."""
        logger.info("Unrealized PnL: %s", self.unrealized_pnl)
        logger.info("Realized PnL: %s", self.realized_pnl)
        logger.info("Current Positions: %s", self.positions)

    def __init__(self):
        self.positions = {}
        self.tax_lots = {}
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0

    def update_position(
        self, stock_symbol: str,
        trade_price: float,
        quantity: int,
        trade_type: TradeState,
        strike: float, premium: float,
        is_zero_dte: bool
    ):
        """Update position tracking for a trade."""
        if stock_symbol not in self.positions:
            self.positions[stock_symbol] = {
                'quantity': 0,
                'avg_price': 0.0,
                'strikes': [],
                'premiums': []
            }

        position = self.positions[stock_symbol]

        if trade_type in (
            TradeState.PUT_SOLD,
            TradeState.CALL_SOLD
        ):
            # Opening trade
            position['quantity'] += quantity
            position['strikes'].append(strike)
            position['premiums'].append(premium)

            # Update average price
            total_cost = position['avg_price'] * \
                (position['quantity'] - quantity)
            position['avg_price'] = (
                total_cost + trade_price * quantity) / position['quantity']

            # Create tax lot
            if stock_symbol not in self.tax_lots:
                self.tax_lots[stock_symbol] = []
            self.tax_lots[stock_symbol].append({
                'open_date': datetime.now(),
                'quantity': quantity,
                'open_price': trade_price,
                'strike': strike,
                'is_zero_dte': is_zero_dte
            })
        else:
            # Closing trade
            position['quantity'] -= quantity
            closed_pnl = (trade_price - position['avg_price']) * quantity
            self.realized_pnl += closed_pnl

            # Update tax lots (FIFO)
            remaining = quantity
            for lot in self.tax_lots.get(stock_symbol, []):
                if remaining <= 0:
                    break
                close_qty = min(remaining, lot['quantity'])
                lot['quantity'] -= close_qty
                remaining -= close_qty
                lot['close_date'] = datetime.now()
                lot['close_price'] = trade_price

        # Update unrealized PnL
        self._update_unrealized_pnl()

    def _update_unrealized_pnl(self):
        """Update unrealized PnL for all positions."""
        self.unrealized_pnl = 0.0
        for symbol, position in self.positions.items():
            current_price = get_underlying_price(symbol)
            for strike in position['strikes']:
                self.unrealized_pnl += (
                    current_price -
                    strike
                ) * position['quantity']


class TradeManager:
    """Manages trade entry, exit, and adjustments."""

    def __init__(self):
        self.open_trades = []

    def should_exit_trade(
        self,
        trade: Dict,
        current_price: float
    ) -> bool:
        """Determine if a trade should be exited."""
        if trade['type'] == ContractType.PUT:
            return current_price >= trade['strike'] * (
                1 + config.target_stop_loss_percentage
            )
        return current_price <= trade['strike'] * (
            1 - config.target_stop_loss_percentage
        )

    def manage_exits(self):
        """Manage all open trades for potential exits."""
        for trade in list(self.open_trades):
            current_price = get_underlying_price(trade['symbol'])
            if self.should_exit_trade(trade, current_price):
                self.exit_trade(trade)

    def exit_trade(self, trade: Dict):
        """Exit a specific trade."""
        logger.info("Exiting trade %s", trade['id'])
        self.open_trades.remove(trade)


class StockSelector:
    """Handles stock selection based on strategy criteria."""

    def __init__(self):
        self.last_scan_time = None
        self.candidate_stocks = []

    @lru_cache(maxsize=100)
    def calculate_volatility(
        self, symbol: str
    ) -> float:
        """Calculate historical volatility for a stock."""
        # Implementation would use real market data
        logger.debug("Calculating volatility for symbol: %s", symbol)
        return 0.25  # Placeholder

    def scan_stocks(self) -> List[Stock]:
        """Scan for stocks meeting criteria."""
        # This would use real market data in production
        example_stocks = [
            Stock("AAPL", 175.50, 5000000, 0.22,
                  date.today() - timedelta(days=30)),
            Stock("MSFT", 325.75, 8000000, 0.18,
                  date.today() - timedelta(days=25))
        ]

        filtered = [
            s for s in example_stocks
            if config.min_stock_price <= s.price <= config.max_stock_price
            and s.avg_volume >= config.min_average_volume
        ]

        self.candidate_stocks = sorted(
            filtered,
            key=lambda x: (-x.volatility, -x.avg_volume)
        )[:config.target_stock_count]

        self.last_scan_time = datetime.now()
        return self.candidate_stocks

# ----------------------- Strategy Implementation -----------------------


class OptionsWheelStrategy:
    """Main implementation of the options wheel strategy."""

    def __init__(self):
        self.stock_selector = StockSelector()
        self.trade_manager = TradeManager()
        self.position_tracker = PositionTracker()
        self.option_cache = OptionCache()
        self.circuit_breaker = CircuitBreaker()
        self.last_execution_time = None
        self.market_volatility = 0.0

    def run_strategy_cycle(self):
        """Execute one complete cycle of the strategy."""
        try:
            if not self._pre_checks():
                return False

            self._update_market_conditions()
            stocks = self.stock_selector.scan_stocks()

            self.trade_manager.manage_exits()

            for stock in stocks:
                self._evaluate_stock(stock)

            self.last_execution_time = datetime.now()
            return True

        except CircuitBreakerError as e:
            logger.error("Circuit breaker blocked execution: %s", str(e))
            return False
        except (ValueError, KeyError, RuntimeError) as e:
            # Replace with specific exceptions
            logger.error("Strategy cycle failed: %s", str(e), exc_info=True)
            self.circuit_breaker.record_failure()
            return False

    def _pre_checks(self) -> bool:
        """Perform pre-execution checks."""
        if not self.circuit_breaker.check():
            return False

        if not health_check():
            logger.warning("Health check failed, skipping cycle")
            return False

        return True

    def _update_market_conditions(self):
        """Update current market conditions."""
        self.market_volatility = self.stock_selector.calculate_volatility(
            "SPY")
        # Adjust configuration based on market volatility
        config.min_premium_return = max(
            config.min_premium_return * (1 + self.market_volatility),
            config.min_premium_return  # type: ignore
        )
        config.max_positions = max(
            int(config.max_positions * (1 - self.market_volatility)),
            1
        )

    def _evaluate_stock(self, stock: Stock):
        """Evaluate a stock for potential trades."""
        if len(self.trade_manager.open_trades) >= config.max_positions:
            return

        if self._calculate_portfolio_risk() >= config.max_portfolio_risk:
            return

        if self._should_trade_puts():
            self._evaluate_put_strategy(stock)

        if (
            self._should_trade_calls() and
            stock.symbol in self.position_tracker.positions
        ):
            self._evaluate_call_strategy(stock)

    def _should_trade_puts(self) -> bool:
        """Determine if we should trade puts."""
        today = datetime.now().weekday()
        return (
            today in config.zero_dte_trade_days
            if config.zero_dte_enabled
            else True
        )

    def _should_trade_calls(self) -> bool:
        """Determine if we should trade calls."""
        today = datetime.now().weekday()
        return (
            today in config.zero_dte_trade_days
            if config.zero_dte_enabled
            else True
        )

    def _evaluate_put_strategy(self, stock: Stock):
        """Evaluate selling puts for a stock."""
        expirations = self._get_available_expirations(stock.symbol)

        for expiration in expirations:
            options = self._get_option_chain(
                stock.symbol, expiration, ContractType.PUT)
            suitable = self._filter_options(options, is_put=True)

            if suitable:
                best_option = self._select_best_option(suitable)
                if best_option:
                    self._execute_trade(
                        best_option, self._is_zero_dte(expiration))

    def _evaluate_call_strategy(self, stock: Stock):
        """Evaluate selling calls for a stock."""
        if stock.symbol not in self.position_tracker.positions:
            return

        expirations = self._get_available_expirations(stock.symbol)

        for expiration in expirations:
            options = self._get_option_chain(
                stock.symbol, expiration, ContractType.CALL)
            suitable = self._filter_options(options, is_put=False)

            if suitable:
                best_option = self._select_best_option(suitable)
                if best_option:
                    self._execute_trade(
                        best_option, self._is_zero_dte(expiration))

    def _get_available_expirations(self, symbol: str) -> List[date]:
        """Get available option expiration dates."""
        logger.debug("Fetching available expirations for symbol: %s", symbol)
        today = date.today()
        return [
            today + timedelta(days=1),  # 1 DTE
            today + timedelta(days=8),  # Weekly
            today + timedelta(days=15)  # Monthly
        ]

    def _get_option_chain(
        self,
        symbol: str,
        expiration: date,
        contract_type: ContractType
    ) -> List[OptionContract]:
        """Get option chain for a symbol and expiration."""
        strikes = self._generate_strikes(symbol, contract_type)
        return [
            OptionContract(
                type=contract_type,
                expiration_date=expiration,
                strike_price=strike,
                symbol=symbol,
                bid=1.25,
                ask=1.35,
                delta=-0.3 if contract_type == ContractType.PUT else 0.3,
                volume=1500,
                open_interest=1200
            )
            for strike in strikes
        ]

    def _generate_strikes(
        self, symbol: str,
        contract_type: ContractType
    ) -> List[float]:
        """Generate strike prices around current price."""
        current_price = get_underlying_price(symbol)
        if contract_type == ContractType.PUT:
            return [
                current_price * (1 - 0.02),
                current_price * (1 - 0.05),
                current_price * (1 - 0.10)
            ]
        return [
            current_price * (1 + 0.02),
            current_price * (1 + 0.05),
            current_price * (1 + 0.10)
        ]

    def _filter_options(
        self,
        options: List[OptionContract],
        is_put: bool
    ) -> List[OptionContract]:
        """Filter options based on strategy criteria."""
        filtered = []

        for option in options:
            min_vol = config.zero_dte_min_volume if self._is_zero_dte(
                option.expiration_date) else config.oi_threshold
            if option.volume < min_vol:
                continue

            delta_range = (
                config.short_put_delta_range
                if is_put
                else config.short_call_delta_range
            )
            if not (
                delta_range[0] <= option.delta <= delta_range[1]
            ):
                continue

            min_prem = config.zero_dte_min_premium if self._is_zero_dte(
                option.expiration_date) else config.min_premium_return
            if (option.bid + option.ask) / 2 < min_prem:
                continue

            filtered.append(option)

        return filtered

    def _select_best_option(
        self,
        options: List[OptionContract]
    ) -> Optional[OptionContract]:
        """Select the best option from filtered candidates."""
        if not options:
            return None

        return sorted(
            options,
            key=lambda x: (x.bid + x.ask) / 2 / x.strike_price,
            reverse=True
        )[0]

    def _execute_trade(
        self,
        option: OptionContract,
        is_zero_dte: bool
    ):
        """Execute a trade for the given option."""
        try:
            position_size = self._calculate_position_size(
                option.strike_price, is_zero_dte)
            mid_price = (option.bid + option.ask) / 2

            if config.dry_run:
                logger.info("DRY RUN: Would execute trade for %s",
                            option.symbol)
                return

            # In a real implementation, this would call your broker API
            executed_price = mid_price  # Simplified for example

            trade_type = (
                TradeState.PUT_SOLD
                if option.type == ContractType.PUT
                else TradeState.CALL_SOLD
            )
            self.position_tracker.update_position(
                stock_symbol=option.symbol,
                trade_price=executed_price,
                quantity=position_size,
                trade_type=trade_type,
                strike=option.strike_price,
                premium=mid_price,
                is_zero_dte=is_zero_dte
            )

            self.trade_manager.open_trades.append({
                'id': (
                    f"{option.symbol}-{option.expiration_date}-"
                    f"{option.strike_price}"
                ),
                'symbol': option.symbol,
                'type': option.type,
                'strike': option.strike_price,
                'expiration': option.expiration_date,
                'quantity': position_size,
                'entry_price': mid_price,
                'entry_time': datetime.now()
            })

            logger.info("Executed trade for %s", option.symbol)
            self.circuit_breaker.record_success()

        except (ValueError, RuntimeError, KeyError) as e:
            # Replace with specific exceptions
            logger.error("Trade execution failed: %s", str(e))
            self.circuit_breaker.record_failure()
            raise

    def _calculate_position_size(
        self, strike: float,
        is_zero_dte: bool
    ) -> int:
        """Calculate appropriate position size."""
        account_size = 100000  # Would come from broker API
        risk_per_trade = account_size * config.position_size
        if is_zero_dte:
            risk_per_trade *= 0.5
        return int(risk_per_trade / strike)

    def _calculate_portfolio_risk(self) -> float:
        """Calculate current portfolio risk."""
        total_at_risk = sum(
            trade['quantity'] * trade['strike']
            for trade in self.trade_manager.open_trades
        )
        account_size = 100000  # Would come from broker API
        return total_at_risk / account_size

    def _is_zero_dte(
        self,
        expiration: date
    ) -> bool:
        """Check if expiration is 0DTE."""
        return (expiration - date.today()).days <= 1

# ----------------------- Utility Functions -----------------------


# Initialize API circuit breaker
class CircuitBreaker:
    """Simple implementation of a circuit breaker."""

    def __init__(
        self,
        failure_threshold=5,
        recovery_time=60
    ):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failure_count = 0
        self.last_failure_time = None

    def record_failure(self):
        """Record a failure in the circuit breaker."""
        self.failure_count += 1
        self.last_failure_time = datetime.now()

    def record_success(self):
        """Record a success in the circuit breaker."""
        self.failure_count = 0
        self.last_failure_time = None

    def check(self) -> bool:
        """Check if the circuit breaker is tripped.

        Returns:
            bool: _description_
        """
        if self.failure_count >= self.failure_threshold:
            if self.last_failure_time and (
                (
                    datetime.now() - self.last_failure_time
                ).seconds < self.recovery_time
            ):
                return False
            self.failure_count = 0
        return True


api_circuit_breaker = CircuitBreaker()


@backoff.on_exception(
    backoff.expo,
    (Exception),
    max_tries=3,
    logger=logger
)
def safe_api_call(api_func, *args, **kwargs):
    """Wrapper with retry logic and circuit breaker."""
    if not api_circuit_breaker.check():
        raise CircuitBreakerError("API circuit breaker tripped")
    try:
        result = api_func(*args, **kwargs)
        api_circuit_breaker.record_success()
        return result
    except (ValueError, RuntimeError, KeyError) as e:
        # Replace with specific exceptions
        logger.error("API call failed: %s", str(e))
        api_circuit_breaker.record_failure()
        raise


def get_underlying_price(symbol: str) -> float:
    """Get current underlying price."""
    # Implementation would call market data API
    logger.debug("Fetching price for symbol: %s", symbol)
    return 100.00  # Example value


def health_check() -> bool:
    """Perform system health check."""
    try:
        # Implementation would check API connectivity, etc.
        return True
    except (ValueError, RuntimeError, KeyError) as e:
        # Replace with specific exceptions
        logger.error("Health check failed: %s", str(e))
        return False

# ----------------------- Monitoring -----------------------


def monitoring_loop(strategy: OptionsWheelStrategy):
    """Continuous monitoring loop for strategy health."""
    while True:
        try:
            if not health_check():
                logger.error("Health check failed, pausing strategy")
                time.sleep(config.health_check_interval * 2)
                continue

            strategy.position_tracker.log_metrics()

            if not strategy.circuit_breaker.check():
                logger.warning("Circuit breaker tripped, waiting to reset")

            time.sleep(config.health_check_interval)

        except (ValueError, RuntimeError, KeyError) as e:
            # Replace with specific exceptions
            logger.error("Monitoring loop failed: %s", str(e), exc_info=True)
            time.sleep(config.health_check_interval)

# ----------------------- Main Execution -----------------------


def main():
    """Initialize and run the strategy."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger.info("Initializing Options Wheel Strategy")

    try:
        strategy = OptionsWheelStrategy()
        monitor_thread = threading.Thread(
            target=monitoring_loop,
            args=(strategy,),
            daemon=True
        )
        monitor_thread.start()

        while True:
            strategy.run_strategy_cycle()
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("Shutting down strategy gracefully")
    except (ValueError, RuntimeError, KeyError) as e:
        logger.error("Strategy crashed: %s", str(e), exc_info=True)


if __name__ == "__main__":
    main()
