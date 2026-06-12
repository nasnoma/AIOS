"""
trading_engine/agents/base.py
Base types shared by all specialist agents.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class AgentSignal:
    agent: str
    signal: Signal
    confidence: float          # 0–100
    reason: str
    weight: float = 1.0        # dynamic weight (updated by judge from track record)
    raw_data: Optional[dict] = None  # optional debug data
