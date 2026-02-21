"""
Data collection module for morning momentum backtesting framework.

This module implements the 3-stage data collection pipeline:
1. Build candidate lists per day using daily bars
2. Download minute bars only for candidate symbol-days
3. Compute features/indicators locally
"""

__version__ = "0.1.0"
