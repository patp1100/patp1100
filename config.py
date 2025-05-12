"""
Options Wheel Strategy Configuration Module
"""

from dataclasses import dataclass, field
from typing import List, Tuple
import logging
from pathlib import Path
import os
import yaml
import appdirs

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = """# Trading parameters
strike_range: 0.05
buy_power_limit: 0.10
risk_free_rate: 0.01
oi_threshold: 200
target_stop_loss_percentage: 0.5
delta_stop_loss_thres: 2.0
short_put_delta_range: [-0.42, -0.18]
short_call_delta_range: [0.18, 0.42]

# Portfolio risk parameters
max_portfolio_risk: 0.10
max_positions: 5
position_size: 0.05

# Premium optimization
min_premium_return: 0.03
days_to_expiration_range: [5, 14]

# 0DTE parameters
zero_dte_enabled: true
zero_dte_min_premium: 0.015
zero_dte_max_risk: 0.05
zero_dte_trade_days: [0, 1, 2, 3]
zero_dte_min_volume: 1000

# Stock selection
min_stock_price: 10.0
max_stock_price: 40.0
target_stock_count: 100
min_average_volume: 500000

# Execution parameters
dry_run: false
max_api_retries: 3
circuit_breaker_failures: 5
circuit_breaker_timeout: 300
health_check_interval: 300
metrics_log_interval: 3600

# Slippage
slippage_model: proportional
slippage_fixed: 0.05
slippage_proportional: 0.001

# Dynamic adjustment
volatility_adjustment_enabled: true
volatility_lookback_days: 20
"""

CONFIG_FILE_NAME = "wheel_config.yaml"


@dataclass
class StrategyConfig:
    """Configuration container for Options Wheel Strategy."""

    # Trading parameters
    strike_range: float = field(default=0.05)
    buy_power_limit: float = field(default=0.10)
    risk_free_rate: float = field(default=0.01)
    oi_threshold: int = field(default=200)
    target_stop_loss_percentage: float = field(default=0.5)
    delta_stop_loss_thres: float = field(default=2.0)
    short_put_delta_range: Tuple[float, float] = field(
        default_factory=lambda: (-0.42, -0.18))
    short_call_delta_range: Tuple[float, float] = field(
        default_factory=lambda: (0.18, 0.42))

    # Portfolio risk parameters
    max_portfolio_risk: float = field(default=0.10)
    max_positions: int = field(default=5)
    position_size: float = field(default=0.05)

    # Premium optimization
    min_premium_return: float = field(default=0.03)
    days_to_expiration_range: Tuple[int, int] = field(
        default_factory=lambda: (5, 14))

    # 0DTE parameters
    zero_dte_enabled: bool = field(default=True)
    zero_dte_min_premium: float = field(default=0.015)
    zero_dte_max_risk: float = field(default=0.05)
    zero_dte_trade_days: List[int] = field(
        default_factory=lambda: [0, 1, 2, 3])
    zero_dte_min_volume: int = field(default=1000)

    # Stock selection
    min_stock_price: float = field(default=10.0)
    max_stock_price: float = field(default=40.0)
    target_stock_count: int = field(default=100)
    min_average_volume: int = field(default=500000)

    # Execution parameters
    dry_run: bool = field(default=False)
    max_api_retries: int = field(default=3)
    circuit_breaker_failures: int = field(default=5)
    circuit_breaker_timeout: int = field(default=300)
    health_check_interval: int = field(default=300)
    metrics_log_interval: int = field(default=3600)

    # Slippage
    slippage_model: str = field(default="proportional")
    slippage_fixed: float = field(default=0.05)
    slippage_proportional: float = field(default=0.001)

    # Dynamic adjustment
    volatility_adjustment_enabled: bool = field(default=True)
    volatility_lookback_days: int = field(default=20)

    @classmethod
    def get_writable_config_path(cls) -> Path:
        """Find a writable location for the config file."""
        locations = [
            # 1. Current working directory
            Path.cwd() / CONFIG_FILE_NAME,
            # 2. Platform-specific config directory
            Path(appdirs.user_config_dir("options_wheel")) / CONFIG_FILE_NAME,
            # 3. User home directory
            Path.home() / f".{CONFIG_FILE_NAME}",
            # 4. Same directory as this file
            Path(__file__).parent / CONFIG_FILE_NAME
        ]

        # Check existing writable configs first
        for path in locations:
            if path.exists() and os.access(path.parent, os.W_OK):
                return path

        # Find first writable location
        for path in locations:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                test_file = path.parent / ".config_test"
                test_file.touch()
                test_file.unlink()
                return path
            except (OSError, PermissionError):
                continue

        # Final fallback - current directory (may fail)
        return Path(CONFIG_FILE_NAME)

    @classmethod
    def create_default_config(cls) -> 'StrategyConfig':
        """Create default config file with comprehensive error handling."""
        config = cls()
        path = cls.get_writable_config_path()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w', encoding='utf-8') as f:
                yaml.safe_dump(config.__dict__, f)
            logger.info("Successfully created config at %s", path)
            return config
        except PermissionError:
            logger.warning("Permission denied creating config at %s", path)
        except (OSError, yaml.YAMLError) as e:
            logger.warning("Failed to create config: %s", str(e))

        logger.info("Using in-memory defaults only")
        return config

    @classmethod
    def load_from_file(cls) -> 'StrategyConfig':
        """Load config with automatic recovery."""
        try:
            path = cls.get_writable_config_path()

            if path.exists():
                with path.open('r', encoding='utf-8') as f:
                    config_data = yaml.safe_load(f) or {}
                    cls._validate_config(config_data)
                    logger.info("Loaded config from %s", path)
                    return cls(**config_data)

            return cls.create_default_config()

        except yaml.YAMLError as e:
            logger.error("Invalid YAML in config: %s", str(e))
        except (FileNotFoundError, PermissionError) as e:
            logger.error("Error loading config: %s", str(e))

        logger.info("Using in-memory defaults")
        return cls()

    @staticmethod
    def _validate_config(config_data: dict) -> None:
        """Validate the configuration data."""
        required_fields = [
            "strike_range", "buy_power_limit", "risk_free_rate",
            "oi_threshold",
            "target_stop_loss_percentage",
            "delta_stop_loss_thres",
            "short_put_delta_range",
            "short_call_delta_range", "max_portfolio_risk",
            "max_positions", "position_size",
            "min_premium_return", "days_to_expiration_range",
            "zero_dte_enabled",
            "zero_dte_min_premium", "zero_dte_max_risk", "zero_dte_trade_days",
            "zero_dte_min_volume", "min_stock_price",
            "max_stock_price", "target_stock_count",
            "min_average_volume", "dry_run", "max_api_retries",
            "circuit_breaker_failures",
            "circuit_breaker_timeout",
            "health_check_interval",
            "metrics_log_interval",
            "slippage_model", "slippage_fixed", "slippage_proportional",
            "volatility_adjustment_enabled", "volatility_lookback_days"
        ]
        for required_field in required_fields:
            if required_field not in config_data:
                raise ValueError(
                    f"Missing required config field: {required_field}"
                )

    # [Rest of the class implementation remains the same]


def get_config() -> StrategyConfig:
    """Public interface to get configuration."""
    try:
        return StrategyConfig.load_from_file()
    except (FileNotFoundError, PermissionError, yaml.YAMLError) as e:
        logger.error("Critical config error: %s", str(e))
        logger.info("Returning safe defaults")
        return StrategyConfig()
