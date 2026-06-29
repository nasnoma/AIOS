"""
polymarket_bot/dashboard.py

Embedded aiohttp web server running a premium quantitative dashboard for Bybit Arbitrage Bot.
Features deep navy dark mode, glassmorphic cards, live state updates, multi-route tickers, and logs.
"""
from __future__ import annotations
import os
import json
import asyncio
from collections import deque
from datetime import datetime, timezone
from aiohttp import web
from loguru import logger

from polymarket_bot.state import load_state
from polymarket_bot.config import settings

# Global deque to store recent logs for the UI console
recent_logs = deque(maxlen=100)

def log_sink(message):
    """Loguru sink that stores formatted log lines for the dashboard."""
    recent_logs.append(message.strip())

# Setup loguru sink
logger.add(log_sink, level="INFO", format="{time:HH:mm:ss} | {level:7} | {message}")

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bybit Spot Arbitrage Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-main: #060913;
            --bg-card: rgba(13, 20, 38, 0.45);
            --bg-card-hover: rgba(20, 30, 58, 0.6);
            --border-color: rgba(255, 255, 255, 0.07);
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --color-primary: #06b6d4;
            --color-primary-glow: rgba(6, 182, 212, 0.15);
            --color-green: #10b981;
            --color-green-glow: rgba(16, 185, 129, 0.15);
            --color-red: #f43f5e;
            --color-red-glow: rgba(244, 63, 94, 0.15);
            --color-orange: #f59e0b;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg-main);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            overflow-x: hidden;
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(6, 182, 212, 0.05) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(99, 102, 241, 0.05) 0%, transparent 40%);
            background-attachment: fixed;
        }

        header {
            padding: 1.5rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            background: rgba(6, 9, 19, 0.8);
            backdrop-filter: blur(12px);
            z-index: 10;
            position: sticky;
            top: 0;
        }

        .logo-container {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-icon {
            width: 2.25rem;
            height: 2.25rem;
            border-radius: 0.5rem;
            background: linear-gradient(135deg, var(--color-primary), #6366f1);
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.2rem;
            box-shadow: 0 0 15px rgba(6, 182, 212, 0.4);
        }

        .logo-text h1 {
            font-size: 1.25rem;
            font-weight: 700;
            letter-spacing: -0.025em;
            background: linear-gradient(to right, #ffffff, #cbd5e1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .logo-text span {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-weight: 400;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .status-badge {
            padding: 0.5rem 1rem;
            border-radius: 2rem;
            font-size: 0.875rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            border: 1px solid rgba(255, 255, 255, 0.05);
            transition: all 0.3s ease;
        }

        .status-badge.active {
            background-color: var(--color-green-glow);
            color: var(--color-green);
            border-color: rgba(16, 185, 129, 0.3);
            box-shadow: 0 0 10px rgba(16, 185, 129, 0.1);
        }

        .status-badge.disconnected {
            background-color: var(--color-red-glow);
            color: var(--color-red);
            border-color: rgba(244, 63, 94, 0.3);
            box-shadow: 0 0 10px rgba(244, 63, 94, 0.1);
        }

        .pulse-dot {
            width: 0.5rem;
            height: 0.5rem;
            border-radius: 50%;
            background-color: currentColor;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% { opacity: 0.4; }
            50% { opacity: 1; }
            100% { opacity: 0.4; }
        }

        main {
            flex: 1;
            padding: 2rem;
            max-width: 1400px;
            width: 100%;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 2rem;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 1.5rem;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 1rem;
            padding: 1.5rem;
            backdrop-filter: blur(20px);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }

        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.02) 0%, transparent 100%);
            pointer-events: none;
        }

        .card:hover {
            transform: translateY(-2px);
            border-color: rgba(255, 255, 255, 0.12);
            background: var(--bg-card-hover);
        }

        .metric-title {
            font-size: 0.875rem;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .metric-value {
            font-size: 1.8rem;
            font-weight: 700;
            letter-spacing: -0.03em;
        }

        .metric-sub {
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.25rem;
        }

        .text-green { color: var(--color-green); }
        .text-red { color: var(--color-red); }

        .dashboard-body {
            display: grid;
            grid-template-columns: 1.6fr 1.4fr;
            gap: 1.5rem;
        }

        @media (max-width: 1024px) {
            .dashboard-body {
                grid-template-columns: 1fr;
            }
        }

        .section-title {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1.25rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .table-container {
            position: relative;
            width: 100%;
            overflow-x: auto;
        }

        .table-container::after {
            content: '';
            position: absolute;
            top: 0;
            right: 0;
            width: 3rem;
            height: 100%;
            background: linear-gradient(to right, transparent, var(--bg-card));
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.2s;
        }

        .table-container.has-overflow::after {
            opacity: 1;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }

        th {
            padding: 0.75rem 1rem;
            color: var(--text-muted);
            font-weight: 600;
            font-size: 0.8rem;
            text-transform: uppercase;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 1rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
        }

        tr:last-child td {
            border-bottom: none;
        }

        .badge {
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }

        .badge.buy { background: var(--color-green-glow); color: var(--color-green); }
        .badge.sell { background: rgba(99, 102, 241, 0.15); color: #818cf8; }

        .console-card {
            display: flex;
            flex-direction: column;
            height: clamp(200px, 45vh, 500px);
        }

        .console-body {
            flex: 1;
            background: rgba(3, 5, 10, 0.8);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 0.5rem;
            padding: 1rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            line-height: 1.5;
            overflow-y: auto;
            white-space: pre-wrap;
            color: #cbd5e1;
        }

        .console-line {
            margin-bottom: 0.4rem;
            border-left: 2px solid transparent;
            padding-left: 0.5rem;
        }

        .console-line.info { border-left-color: var(--color-primary); }
        .console-line.warning { border-left-color: var(--color-orange); color: var(--color-orange); }
        .console-line.success { border-left-color: var(--color-green); color: var(--color-green); }
        .console-line.error { border-left-color: var(--color-red); color: var(--color-red); }

        .spot-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }

        .ticker-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 1rem;
            text-align: center;
        }

        .ticker-name {
            font-size: 0.8rem;
            color: var(--text-muted);
            font-weight: 600;
            margin-bottom: 0.5rem;
        }

        .ticker-price {
            font-size: 1.15rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }

        .arb-monitor {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }

        .arb-path {
            background: rgba(6, 182, 212, 0.03);
            border: 1px solid rgba(6, 182, 212, 0.1);
            border-radius: 0.75rem;
            padding: 1rem;
            text-align: center;
        }

        .arb-path.reverse {
            background: rgba(99, 102, 241, 0.03);
            border: 1px solid rgba(99, 102, 241, 0.1);
        }

        .arb-path-title {
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.25rem;
        }

        @media (max-width: 768px) {
            main {
                padding: 1rem;
                gap: 1.25rem;
            }
            header {
                padding: 1rem;
            }
            .spot-grid {
                grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
                gap: 0.75rem;
            }
            .arb-monitor {
                grid-template-columns: 1fr;
                gap: 0.75rem;
            }
            .metrics-grid {
                grid-template-columns: 1fr 1fr;
                gap: 1rem;
            }
            .logo-text h1 {
                font-size: 1.1rem;
            }
            .logo-text span {
                font-size: 0.65rem;
            }
            .metric-value {
                font-size: 1.4rem;
            }
            .arb-path-edge {
                font-size: 1.2rem;
            }
        }
        @media (max-width: 480px) {
            .metrics-grid {
                grid-template-columns: 1fr;
            }
            header {
                flex-direction: row;
                justify-content: space-between;
                align-items: center;
                gap: 0.5rem;
            }
            .logo-text span {
                display: none; /* Hide subtitle to save horizontal space on extremely small screens */
            }
            .status-badge {
                padding: 0.35rem 0.75rem;
                font-size: 0.75rem;
            }
        }

        .arb-path-edge {
            font-size: 1.4rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-container">
            <div class="logo-icon">▲</div>
            <div class="logo-text">
                <h1>Bybit Spot Arbitrage</h1>
                <span>Halal Triangular Engine</span>
            </div>
        </div>
        <div class="status-badge active" id="bot-status">
            <div class="pulse-dot"></div>
            <span id="bot-status-text">Active</span>
        </div>
    </header>

    <main>
        <!-- Metrics -->
        <div class="metrics-grid">
            <div class="card">
                <div class="metric-title">Live Balance</div>
                <div class="metric-value" id="cash-val">---</div>
                <div class="metric-sub">USDT Spot Account</div>
            </div>
            <div class="card">
                <div class="metric-title">Daily P&L</div>
                <div class="metric-value" id="daily-pnl">---</div>
                <div class="metric-sub" id="daily-pnl-sub">Realized today</div>
            </div>
            <div class="card">
                <div class="metric-title">Total P&L</div>
                <div class="metric-value" id="total-pnl">---</div>
                <div class="metric-sub" id="total-pnl-sub">Cumulative</div>
            </div>
            <div class="card">
                <div class="metric-title">Win Rate</div>
                <div class="metric-value" id="win-rate">---</div>
                <div class="metric-sub" id="win-loss-count">-- W / -- L</div>
            </div>
            <div class="card">
                <div class="metric-title">Avg Win / Loss</div>
                <div class="metric-value" id="avg-win-loss">---</div>
                <div class="metric-sub" id="ev-val">EV/Cycle: ---</div>
            </div>
        </div>

        <!-- Real-time Orderbooks -->
        <div class="section-title">
            <span>Market Monitor</span>
            <span style="font-size: 0.8rem; font-weight: normal; color: var(--text-muted);" id="last-updated">Updated just now</span>
        </div>
        
        <div class="spot-grid">
            <div class="ticker-card">
                <div class="ticker-name">BTC/USDT</div>
                <div class="ticker-price" id="ticker-btcusdt">---</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name">ETH/USDT</div>
                <div class="ticker-price" id="ticker-ethusdt">---</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name">ETH/BTC</div>
                <div class="ticker-price" id="ticker-ethbtc">---</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name">SOL/USDT</div>
                <div class="ticker-price" id="ticker-solusdt">---</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name">SOL/BTC</div>
                <div class="ticker-price" id="ticker-solbtc">---</div>
            </div>
        </div>

        <div class="arb-monitor">
            <div class="arb-path">
                <div class="arb-path-title">BTC-ETH Forward (USDT → BTC → ETH → USDT)</div>
                <div class="arb-path-edge" id="edge-eth-forward">---</div>
            </div>
            <div class="arb-path reverse">
                <div class="arb-path-title">BTC-ETH Reverse (USDT → ETH → BTC → USDT)</div>
                <div class="arb-path-edge" id="edge-eth-reverse">---</div>
            </div>
            <div class="arb-path">
                <div class="arb-path-title">BTC-SOL Forward (USDT → BTC → SOL → USDT)</div>
                <div class="arb-path-edge" id="edge-sol-forward">---</div>
            </div>
            <div class="arb-path reverse">
                <div class="arb-path-title">BTC-SOL Reverse (USDT → SOL → BTC → USDT)</div>
                <div class="arb-path-edge" id="edge-sol-reverse">---</div>
            </div>
        </div>

        <!-- Route Performance -->
        <div class="section-title">
            <span>Route Performance Breakdown</span>
        </div>
        <div class="spot-grid">
            <div class="ticker-card">
                <div class="ticker-name" style="color: var(--color-primary);">BTC-ETH Forward</div>
                <div class="ticker-price" id="perf-btc-eth-forward">0 W / 0 L (0.0000 USDT)</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name" style="color: #818cf8;">BTC-ETH Reverse</div>
                <div class="ticker-price" id="perf-btc-eth-reverse">0 W / 0 L (0.0000 USDT)</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name" style="color: var(--color-primary);">BTC-SOL Forward</div>
                <div class="ticker-price" id="perf-btc-sol-forward">0 W / 0 L (0.0000 USDT)</div>
            </div>
            <div class="ticker-card">
                <div class="ticker-name" style="color: #818cf8;">BTC-SOL Reverse</div>
                <div class="ticker-price" id="perf-btc-sol-reverse">0 W / 0 L (0.0000 USDT)</div>
            </div>
        </div>

        <!-- Body -->
        <div class="dashboard-body">
            <!-- Left: Executed Cycles -->
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="section-title">
                    <span>Recent Cycles</span>
                    <span style="font-size: 0.8rem; color: var(--text-muted);" id="cycle-count">0 completed</span>
                </div>
                <div class="table-container" id="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Cycle ID</th>
                                <th>Direction</th>
                                <th>Size</th>
                                <th>Expected Edge</th>
                                <th>Realized P&L</th>
                            </tr>
                        </thead>
                        <tbody id="trade-history">
                            <tr>
                                <td colspan="5" style="text-align: center; color: var(--text-muted);">No cycles executed yet</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Right: Log Console -->
            <div class="card console-card">
                <div class="section-title">
                    <span>Real-time Logs</span>
                </div>
                <div class="console-body" id="log-console">
                    <div class="console-line info">Waiting for logs...</div>
                </div>
            </div>
        </div>
    </main>

    <script>
        const tableContainer = document.getElementById('table-container');
        function checkTableOverflow() {
            if (tableContainer.scrollWidth > tableContainer.clientWidth) {
                tableContainer.classList.add('has-overflow');
            } else {
                tableContainer.classList.remove('has-overflow');
            }
        }
        window.addEventListener('resize', checkTableOverflow);

        let disconnectCount = 0;
        let lastPollTime = Date.now();

        async function updateDashboard() {
            try {
                const response = await fetch('/api/state');
                const data = await response.json();
                
                disconnectCount = 0;
                lastPollTime = Date.now();
                document.getElementById('bot-status').className = 'status-badge active';
                document.getElementById('bot-status-text').innerText = 'Active';

                const state = data.state;
                const market = data.market_status;

                // Basic Stats
                document.getElementById('cash-val').innerText = `${state.cash.toFixed(2)} USDT`;
                
                const dPnl = state.daily_pnl;
                const dEl = document.getElementById('daily-pnl');
                dEl.innerText = `${dPnl >= 0 ? '+' : ''}${dPnl.toFixed(4)} USDT`;
                dEl.className = `metric-value ${dPnl >= 0 ? 'text-green' : 'text-red'}`;

                const tPnl = state.total_pnl;
                const tEl = document.getElementById('total-pnl');
                tEl.innerText = `${tPnl >= 0 ? '+' : ''}${tPnl.toFixed(4)} USDT`;
                tEl.className = `metric-value ${tPnl >= 0 ? 'text-green' : 'text-red'}`;

                const totalCycles = state.win_count + state.loss_count;
                const winRate = totalCycles > 0 ? (state.win_count / totalCycles * 100) : 0;
                document.getElementById('win-rate').innerText = `${winRate.toFixed(1)}%`;
                document.getElementById('win-loss-count').innerText = `${state.win_count} W / ${state.loss_count} L`;

                const avgWin = state.avg_win_usd;
                const avgLoss = state.avg_loss_usd;
                document.getElementById('avg-win-loss').innerText = `+${avgWin.toFixed(4)} / ${avgLoss.toFixed(4)}`;
                
                const ev = (winRate / 100 * avgWin) + ((1 - winRate / 100) * avgLoss);
                document.getElementById('ev-val').innerText = `EV/Cycle: ${ev >= 0 ? '+' : ''}${ev.toFixed(4)} USDT`;

                document.getElementById('cycle-count').innerText = `${state.cycle_count} completed`;

                // Route Performance Stats
                const stats = state.route_stats || {};
                const showPerf = (id, key) => {
                    const r = stats[key] || { win_count: 0, loss_count: 0, total_pnl: 0.0 };
                    const el = document.getElementById(id);
                    if (el) {
                        el.innerText = `${r.win_count} W / ${r.loss_count} L (${r.total_pnl >= 0 ? '+' : ''}${r.total_pnl.toFixed(4)} USDT)`;
                        el.className = `ticker-price ${r.total_pnl >= 0 ? 'text-green' : 'text-red'}`;
                    }
                };
                showPerf('perf-btc-eth-forward', 'BTC-ETH-FORWARD');
                showPerf('perf-btc-eth-reverse', 'BTC-ETH-REVERSE');
                showPerf('perf-btc-sol-forward', 'BTC-SOL-FORWARD');
                showPerf('perf-btc-sol-reverse', 'BTC-SOL-REVERSE');

                // Tickers
                if (market.BTCUSDT) {
                    document.getElementById('ticker-btcusdt').innerText = `${market.BTCUSDT.bid.toFixed(2)} / ${market.BTCUSDT.ask.toFixed(2)}`;
                }
                if (market.ETHUSDT) {
                    document.getElementById('ticker-ethusdt').innerText = `${market.ETHUSDT.bid.toFixed(2)} / ${market.ETHUSDT.ask.toFixed(2)}`;
                }
                if (market.ETHBTC) {
                    document.getElementById('ticker-ethbtc').innerText = `${market.ETHBTC.bid.toFixed(5)} / ${market.ETHBTC.ask.toFixed(5)}`;
                }
                if (market.SOLUSDT) {
                    document.getElementById('ticker-solusdt').innerText = `${market.SOLUSDT.bid.toFixed(2)} / ${market.SOLUSDT.ask.toFixed(2)}`;
                }
                if (market.SOLBTC) {
                    document.getElementById('ticker-solbtc').innerText = `${market.SOLBTC.bid.toFixed(5)} / ${market.SOLBTC.ask.toFixed(5)}`;
                }

                // Edges: BTC-ETH
                const edgeEthF = data.edges["BTC-ETH"].forward;
                const edgeEthFE = document.getElementById('edge-eth-forward');
                edgeEthFE.innerText = `${edgeEthF >= 0 ? '+' : ''}${edgeEthF.toFixed(4)}%`;
                edgeEthFE.className = `arb-path-edge ${edgeEthF >= 0 ? 'text-green' : 'text-red'}`;

                const edgeEthR = data.edges["BTC-ETH"].reverse;
                const edgeEthRE = document.getElementById('edge-eth-reverse');
                edgeEthRE.innerText = `${edgeEthR >= 0 ? '+' : ''}${edgeEthR.toFixed(4)}%`;
                edgeEthRE.className = `arb-path-edge ${edgeEthR >= 0 ? 'text-green' : 'text-red'}`;

                // Edges: BTC-SOL
                const edgeSolF = data.edges["BTC-SOL"].forward;
                const edgeSolFE = document.getElementById('edge-sol-forward');
                edgeSolFE.innerText = `${edgeSolF >= 0 ? '+' : ''}${edgeSolF.toFixed(4)}%`;
                edgeSolFE.className = `arb-path-edge ${edgeSolF >= 0 ? 'text-green' : 'text-red'}`;

                const edgeSolR = data.edges["BTC-SOL"].reverse;
                const edgeSolRE = document.getElementById('edge-sol-reverse');
                edgeSolRE.innerText = `${edgeSolR >= 0 ? '+' : ''}${edgeSolR.toFixed(4)}%`;
                edgeSolRE.className = `arb-path-edge ${edgeSolR >= 0 ? 'text-green' : 'text-red'}`;

                // Trade History
                const tbody = document.getElementById('trade-history');
                if (state.closed_trades && state.closed_trades.length > 0) {
                    tbody.innerHTML = state.closed_trades.slice().reverse().map(trade => {
                        const isWin = trade.pnl_usdt >= 0;
                        return `
                            <tr>
                                <td><code>${trade.cycle_id}</code></td>
                                <td><span class="badge buy">${trade.direction}</span></td>
                                <td>${trade.size_usdt.toFixed(2)} USDT</td>
                                <td>${(trade.est_edge_pct * 100).toFixed(4)}%</td>
                                <td class="${isWin ? 'text-green' : 'text-red'} font-mono">${trade.pnl_usdt >= 0 ? '+' : ''}${trade.pnl_usdt.toFixed(4)} USDT</td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No cycles executed yet</td></tr>';
                }
                checkTableOverflow();
            } catch (err) {
                console.error("Dashboard poll failed", err);
                disconnectCount++;
                if (disconnectCount >= 2) {
                    document.getElementById('bot-status').className = 'status-badge disconnected';
                    document.getElementById('bot-status-text').innerText = '⚠ Disconnected';
                }
            }
        }

        async function updateLogs() {
            try {
                const response = await fetch('/api/logs');
                const data = await response.json();
                const consoleEl = document.getElementById('log-console');
                
                const currentScroll = consoleEl.scrollTop;
                const isAtBottom = (consoleEl.scrollHeight - consoleEl.clientHeight - currentScroll) < 30;

                consoleEl.innerHTML = data.logs.map(line => {
                    let levelClass = 'info';
                    if (line.includes('| WARNING |')) levelClass = 'warning';
                    if (line.includes('| ERROR   |')) levelClass = 'error';
                    if (line.includes('Completed!') || line.includes('Executed') || line.includes('Arb Found')) levelClass = 'success';
                    return `<div class="console-line ${levelClass}">${line}</div>`;
                }).join('');

                if (isAtBottom) {
                    consoleEl.scrollTop = consoleEl.scrollHeight;
                }
            } catch (err) {
                console.error("Log poll failed", err);
            }
        }

        // Poll timers
        setInterval(updateDashboard, 1000);
        setInterval(updateLogs, 1000);
        
        // Initial setup
        updateDashboard();
        updateLogs();

        // Update seconds ago timer
        setInterval(() => {
            const sec = Math.floor((Date.now() - lastPollTime) / 1000);
            document.getElementById('last-updated').innerText = sec <= 1 ? 'Updated just now' : `Updated ${sec}s ago`;
        }, 1000);
    </script>
</body>
</html>
"""

async def handle_dashboard_index(request):
    return web.Response(text=_INDEX_HTML, content_type="text/html")

async def handle_api_state(request):
    try:
        price_feed = request.app["price_feed"]
        
        # Calculate raw forward and reverse edges for display
        total_fees = 3 * 0.0010

        # BTC-ETH
        bid_btc, ask_btc, _, _ = price_feed.get_best_bid_ask("BTCUSDT")
        bid_eth, ask_eth, _, _ = price_feed.get_best_bid_ask("ETHUSDT")
        bid_ethbtc, ask_ethbtc, _, _ = price_feed.get_best_bid_ask("ETHBTC")

        forward_edge_eth = 0.0
        reverse_edge_eth = 0.0
        if all([bid_btc, ask_btc, bid_eth, ask_eth, bid_ethbtc, ask_ethbtc]):
            forward_gross_eth = (1.0 / ask_btc) * (1.0 / ask_ethbtc) * bid_eth
            forward_edge_eth = ((forward_gross_eth - 1.0) - total_fees) * 100.0

            reverse_gross_eth = (1.0 / ask_eth) * bid_ethbtc * bid_btc
            reverse_edge_eth = ((reverse_gross_eth - 1.0) - total_fees) * 100.0

        # BTC-SOL
        bid_sol, ask_sol, _, _ = price_feed.get_best_bid_ask("SOLUSDT")
        bid_solbtc, ask_solbtc, _, _ = price_feed.get_best_bid_ask("SOLBTC")

        forward_edge_sol = 0.0
        reverse_edge_sol = 0.0
        if all([bid_btc, ask_btc, bid_sol, ask_sol, bid_solbtc, ask_solbtc]):
            forward_gross_sol = (1.0 / ask_btc) * (1.0 / ask_solbtc) * bid_sol
            forward_edge_sol = ((forward_gross_sol - 1.0) - total_fees) * 100.0

            reverse_gross_sol = (1.0 / ask_sol) * bid_solbtc * bid_btc
            reverse_edge_sol = ((reverse_gross_sol - 1.0) - total_fees) * 100.0

        state = load_state()
        
        market_status = {}
        for asset in ["BTCUSDT", "ETHUSDT", "ETHBTC", "SOLUSDT", "SOLBTC"]:
            bid, ask, _, _ = price_feed.get_best_bid_ask(asset)
            market_status[asset] = {"bid": bid, "ask": ask}

        return web.json_response({
            "state": state.to_dict(),
            "edges": {
                "BTC-ETH": {"forward": forward_edge_eth, "reverse": reverse_edge_eth},
                "BTC-SOL": {"forward": forward_edge_sol, "reverse": reverse_edge_sol},
            },
            "market_status": market_status,
        })
    except Exception as e:
        logger.warning(f"Error compiling api state: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_api_logs(request):
    return web.json_response({"logs": list(recent_logs)})

async def start_dashboard(price_feed) -> web.AppRunner:
    """Initialize and start the dashboard web server on port 8080."""
    app = web.Application()
    app["price_feed"] = price_feed
    
    app.router.add_get("/", handle_dashboard_index)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/api/logs", handle_api_logs)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    logger.info(f"🟢 Dashboard UI running on http://0.0.0.0:{port}")
    return runner
