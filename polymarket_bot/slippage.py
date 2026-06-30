"""
polymarket_bot/slippage.py

Dynamic slippage and fee simulation helper functions.
"""
from __future__ import annotations
import random

def calculate_slippage(quote_size_usd: float, volatility: float, is_solana_leg: bool) -> float:
    """
    Calculates realistic slippage percentage.
    - is_solana_leg: True if Solana DEX, False if Bybit CEX.
    """
    base = 0.0008 if is_solana_leg else 0.0004
    size_factor = min(quote_size_usd / 1000.0, 3.0) * 0.0003
    return base + size_factor + (volatility * 0.001)


def calculate_solana_priority_fee(volatility: float) -> float:
    """
    Simulates a random Solana priority fee between $0.0005 and $0.015,
    scaling higher during high-volatility simulated network congestion.
    """
    base_fee = random.uniform(0.0005, 0.003)
    congestion_fee = min(volatility * 5.0, 1.0) * random.uniform(0.005, 0.012)
    return base_fee + congestion_fee
