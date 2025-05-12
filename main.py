"""_summary_
"""
import time

from config import get_config
from strategy import OptionsWheelStrategy


def main():
    """_summary_
    """
    # Initialize configuration
    config = get_config()

    # Example usage of the config variable
    print("Configuration loaded:", config)

    # Create strategy instance
    strategy = OptionsWheelStrategy()

    # Main loop
    while True:
        strategy.run_cycle()
        time.sleep(60)  # Or use config.polling_interval


if __name__ == "__main__":
    main()
