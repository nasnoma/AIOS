"""
polymarket_bot/dashboard.py

Embedded web server and premium quantitative UI for Bybit-Solana CEX-DEX Arbitrage.
Displays split wallets, real-time spatial spreads, and completed arbitrage cycles.
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
    recent_logs.append(message.strip())

# Setup loguru sink
logger.add(log_sink, level="INFO", format="{time:HH:mm:ss} | {level:7} | {message}")

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bybit-Solana CEX-DEX Arbitrage Bot</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-main: #03050a;
            --bg-card: rgba(8, 12, 24, 0.5);
            --bg-card-hover: rgba(14, 20, 38, 0.65);
            --border-color: rgba(255, 255, 255, 0.05);
            --text-main: #f8fafc;
            --text-muted: #64748b;
            --color-primary: #06b6d4;
            --color-primary-glow: rgba(6, 182, 212, 0.15);
            --color-green: #10b981;
            --color-green-glow: rgba(16, 185, 129, 0.15);
            --color-red: #f43f5e;
            --color-red-glow: rgba(244, 63, 94, 0.15);
            --color-purple: #a855f7;
            --color-purple-glow: rgba(168, 85, 247, 0.15);
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
            background-image: 
                radial-gradient(circle at 5% 10%, rgba(6, 182, 212, 0.05) 0%, transparent 35%),
                radial-gradient(circle at 95% 90%, rgba(168, 85, 247, 0.05) 0%, transparent 35%);
            background-attachment: fixed;
        }

        header {
            padding: 1.25rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            background: rgba(3, 5, 10, 0.8);
            backdrop-filter: blur(12px);
            position: sticky;
            top: 0;
            z-index: 10;
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
            background: linear-gradient(135deg, var(--color-primary), var(--color-purple));
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.2rem;
            box-shadow: 0 0 15px rgba(6, 182, 212, 0.25);
        }

        .logo-text h1 {
            font-size: 1.2rem;
            font-weight: 700;
            background: linear-gradient(to right, #ffffff, #cbd5e1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .logo-text span {
            font-size: 0.7rem;
            color: var(--text-muted);
            font-weight: 400;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .status-badge {
            padding: 0.5rem 1rem;
            border-radius: 2rem;
            font-size: 0.8rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            border: 1px solid rgba(255, 255, 255, 0.03);
            background: rgba(255, 255, 255, 0.01);
        }

        .status-badge.active {
            background-color: var(--color-green-glow);
            color: var(--color-green);
            border-color: rgba(16, 185, 129, 0.15);
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
            gap: 1.75rem;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 1.25rem;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 1.25rem;
            backdrop-filter: blur(15px);
            transition: all 0.3s ease;
        }

        .card:hover {
            border-color: rgba(255, 255, 255, 0.08);
            background: var(--bg-card-hover);
            transform: translateY(-1px);
        }

        .metric-title {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.5rem;
        }

        .metric-value {
            font-size: 1.6rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .metric-sub {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 0.4rem;
        }

        .text-green { color: var(--color-green); }
        .text-red { color: var(--color-red); }

        .dashboard-body {
            display: grid;
            grid-template-columns: 1fr 1.2fr 1.2fr;
            gap: 1.5rem;
        }

        @media (max-width: 1200px) {
            .dashboard-body {
                grid-template-columns: 1fr;
            }
        }

        .section-title {
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        /* Spatial Spreads List */
        .spread-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .spread-row {
            padding: 1rem;
            border-radius: 0.5rem;
            border: 1px solid var(--border-color);
            background: rgba(255, 255, 255, 0.01);
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .spread-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: 600;
            font-size: 0.85rem;
        }

        .spread-detail {
            display: flex;
            justify-content: space-between;
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
        }

        .spread-pct {
            font-size: 1.1rem;
            font-weight: 700;
        }

        /* Trades table & Console Logs */
        .table-container {
            width: 100%;
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
        }

        th {
            text-align: left;
            padding: 0.75rem 1rem;
            color: var(--text-muted);
            font-size: 0.7rem;
            text-transform: uppercase;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 0.8rem 1rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.01);
        }

        .badge {
            display: inline-block;
            padding: 0.15rem 0.35rem;
            border-radius: 0.25rem;
            font-size: 0.65rem;
            font-weight: 600;
        }

        .badge.route-a { background: var(--color-primary-glow); color: var(--color-primary); }
        .badge.route-b { background: var(--color-purple-glow); color: var(--color-purple); }

        .console-card {
            display: flex;
            flex-direction: column;
            height: clamp(300px, 50vh, 500px);
        }

        .console-body {
            flex: 1;
            background: #010204;
            border: 1px solid rgba(255, 255, 255, 0.02);
            border-radius: 0.5rem;
            padding: 1rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            overflow-y: auto;
            white-space: pre-wrap;
            color: #cbd5e1;
        }

        .console-line {
            margin-bottom: 0.3rem;
            border-left: 2px solid transparent;
            padding-left: 0.5rem;
        }

        .console-line.info { border-left-color: var(--color-primary); }
        .console-line.warning { border-left-color: var(--color-purple); color: var(--color-purple); }
        .console-line.success { border-left-color: var(--color-green); color: var(--color-green); }
        .console-line.error { border-left-color: var(--color-red); color: var(--color-red); }

        @media (max-width: 768px) {
            main { padding: 1rem; }
            .metrics-grid { grid-template-columns: 1fr 1fr; }
        }
        @media (max-width: 480px) {
            .metrics-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-container">
            <div class="logo-icon">⇄</div>
            <div class="logo-text">
                <h1>Bybit-Solana CEX-DEX</h1>
                <span>Arbitrage Spatiale Engine</span>
            </div>
        </div>
        <div class="status-badge active" id="bot-status">
            <div class="pulse-dot"></div>
            <span id="bot-status-text">Active</span>
        </div>
    </header>

    <main>
        <!-- Metrics Grid -->
        <div class="metrics-grid">
            <div class="card">
                <div class="metric-title">Account Equity (USDT)</div>
                <div class="metric-value" id="equity-val">---</div>
                <div class="metric-sub" id="equity-sub">Combined Wallet Valuations</div>
            </div>
            <div class="card">
                <div class="metric-title">Bybit Wallet</div>
                <div class="metric-value" id="cex-cash-val">---</div>
                <div class="metric-sub" id="cex-asset-val">--- SOL</div>
            </div>
            <div class="card">
                <div class="metric-title">Solana Wallet</div>
                <div class="metric-value" id="dex-cash-val">---</div>
                <div class="metric-sub" id="dex-asset-val">--- SOL</div>
            </div>
            <div class="card">
                <div class="metric-title">Realized Arbitrage P&L</div>
                <div class="metric-value" id="pnl-val">---</div>
                <div class="metric-sub" id="daily-pnl-val">Daily P&L: ---</div>
            </div>
        </div>

        <!-- Realism Metrics Grid -->
        <div class="metrics-grid" style="margin-top: -0.5rem;">
            <div class="card">
                <div class="metric-title">Cumulative Slippage</div>
                <div class="metric-value" id="slip-val">---</div>
                <div class="metric-sub" id="slip-sub">Total Slippage Cost: ---</div>
            </div>
            <div class="card">
                <div class="metric-title">DEX → CEX Route PnL</div>
                <div class="metric-value" id="route-a-pnl">---</div>
                <div class="metric-sub" id="route-a-winrate">Win Rate: ---</div>
            </div>
            <div class="card">
                <div class="metric-title">CEX → DEX Route PnL</div>
                <div class="metric-value" id="route-b-pnl">---</div>
                <div class="metric-sub" id="route-b-winrate">Win Rate: ---</div>
            </div>
            <div class="card">
                <div class="metric-title">Drawdown / priority fees</div>
                <div class="metric-value" id="drawdown-val">---</div>
                <div class="metric-sub" id="fees-sub">Fees Paid: ---</div>
            </div>
        </div>

        <!-- Rebalancing Card -->
        <div class="card" style="display: flex; flex-direction: column; gap: 0.75rem; background: rgba(8, 12, 24, 0.35);">
            <div class="section-title">
                <span>🔄 Simulate Portfolio Rebalancing</span>
            </div>
            <div style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: center;">
                <div style="display: flex; flex-direction: column; gap: 0.25rem;">
                    <label style="font-size: 0.75rem; color: var(--text-muted);">Asset</label>
                    <select id="rebalance-asset" style="background: var(--bg-main); color: var(--text-main); border: 1px solid var(--border-color); padding: 0.5rem; border-radius: 0.25rem; outline: none; cursor: pointer;">
                        <option value="USDT">USDT</option>
                        <option value="SOL">SOL</option>
                    </select>
                </div>
                <div style="display: flex; flex-direction: column; gap: 0.25rem;">
                    <label style="font-size: 0.75rem; color: var(--text-muted);">Direction</label>
                    <select id="rebalance-dir" style="background: var(--bg-main); color: var(--text-main); border: 1px solid var(--border-color); padding: 0.5rem; border-radius: 0.25rem; outline: none; cursor: pointer;">
                        <option value="CEX_TO_DEX">CEX → DEX (Deducts CEX Withdrawal Fee)</option>
                        <option value="DEX_TO_CEX">DEX → CEX (Deducts On-Chain SOL Gas)</option>
                    </select>
                </div>
                <div style="display: flex; flex-direction: column; gap: 0.25rem;">
                    <label style="font-size: 0.75rem; color: var(--text-muted);">Amount</label>
                    <input type="number" id="rebalance-amt" value="50" step="any" style="background: var(--bg-main); color: var(--text-main); border: 1px solid var(--border-color); padding: 0.5rem; border-radius: 0.25rem; width: 120px; outline: none;">
                </div>
                <button onclick="triggerRebalance()" style="background: linear-gradient(135deg, var(--color-primary), var(--color-purple)); color: white; border: none; padding: 0.6rem 1.2rem; border-radius: 0.25rem; font-weight: 600; cursor: pointer; transition: opacity 0.2s; margin-top: 1rem;">
                    Execute Rebalance
                </button>
                <div id="rebalance-status" style="font-size: 0.85rem; margin-top: 1rem; font-weight: 600;"></div>
            </div>
        </div>

        <!-- 3-Column Layout -->
        <div class="dashboard-body">
            <!-- 1. Real-time Spatial Spreads -->
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="section-title">
                    <span>Spatial Spreads</span>
                    <span style="font-size: 0.75rem; color: var(--text-muted);" id="last-updated">Updated just now</span>
                </div>
                <div class="spread-list">
                    <div class="spread-row">
                        <div class="spread-header">
                            <span>DEX-BUY / CEX-SELL</span>
                            <span class="spread-pct" id="spread-a">---</span>
                        </div>
                        <div class="spread-detail">
                            <span>DEX Swap Cost:</span>
                            <span id="price-dex-buy">---</span>
                        </div>
                        <div class="spread-detail">
                            <span>CEX Bid Price:</span>
                            <span id="price-cex-sell">---</span>
                        </div>
                    </div>

                    <div class="spread-row">
                        <div class="spread-header">
                            <span>CEX-BUY / DEX-SELL</span>
                            <span class="spread-pct" id="spread-b">---</span>
                        </div>
                        <div class="spread-detail">
                            <span>CEX Ask Price:</span>
                            <span id="price-cex-buy">---</span>
                        </div>
                        <div class="spread-detail">
                            <span>DEX Swap Value:</span>
                            <span id="price-dex-sell">---</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 2. Recent Arbitrage Cycles -->
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="section-title">
                    <span>Closed Arbitrage Cycles</span>
                    <span style="font-size: 0.75rem; color: var(--text-muted);" id="fills-count">0 completed</span>
                </div>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Cycle ID</th>
                                <th>Route</th>
                                <th>Size</th>
                                <th>Expected</th>
                                <th>Actual P&L</th>
                                <th>Slippage</th>
                            </tr>
                        </thead>
                        <tbody id="trade-history">
                            <tr>
                                <td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2rem 0;">No arbitrage events logged yet</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 3. Logs Console -->
            <div class="card console-card">
                <div class="section-title">
                    <span>Arbitrage Execution Console</span>
                </div>
                <div class="console-body" id="log-console">
                    <div class="console-line info">Initialising engine consoles...</div>
                </div>
            </div>
        </div>
    </main>

    <script>
        let disconnectCount = 0;
        let lastPollTime = Date.now();

        async function triggerRebalance() {
            const asset = document.getElementById('rebalance-asset').value;
            const direction = document.getElementById('rebalance-dir').value;
            const amount = parseFloat(document.getElementById('rebalance-amt').value);
            const statusEl = document.getElementById('rebalance-status');
            
            statusEl.className = '';
            statusEl.innerText = 'Executing rebalance...';
            
            try {
                const res = await fetch('/api/rebalance', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ asset, direction, amount })
                });
                const data = await res.json();
                if (data.success) {
                    statusEl.style.color = '#10b981';
                    statusEl.innerText = data.message;
                    setTimeout(() => { statusEl.innerText = ''; }, 5000);
                    updateDashboard();
                } else {
                    statusEl.style.color = '#f43f5e';
                    statusEl.innerText = `Error: ${data.error}`;
                }
            } catch (err) {
                statusEl.style.color = '#f43f5e';
                statusEl.innerText = `Error: ${err.message}`;
            }
        }

        async function updateDashboard() {
            try {
                const response = await fetch('/api/state');
                const data = await response.json();
                
                disconnectCount = 0;
                lastPollTime = Date.now();

                const state = data.state;
                const bestBid = data.bybit_bid;
                const bestAsk = data.bybit_ask;
                const dexBuy = data.dex_buy;
                const dexSell = data.dex_sell;
                const cumSlipPct = data.cumulative_slippage_pct || 0.0;
                const totalSlipUsd = state.total_slippage_usd || 0.0;

                // Bot Status Badge (Handle Max Drawdown Pause)
                if (state.max_drawdown_paused) {
                    document.getElementById('bot-status').className = 'status-badge disconnected';
                    document.getElementById('bot-status-text').innerText = 'PAUSED (Drawdown Guard)';
                } else {
                    document.getElementById('bot-status').className = 'status-badge active';
                    document.getElementById('bot-status-text').innerText = 'Active';
                }

                // 1. Basic Stats
                document.getElementById('equity-val').innerText = `${state.account_size.toFixed(2)} USDT`;
                document.getElementById('cex-cash-val').innerText = `${state.cex_cash.toFixed(2)} USDT`;
                document.getElementById('cex-asset-val').innerText = `${state.cex_asset.toFixed(4)} SOL`;
                document.getElementById('dex-cash-val').innerText = `${state.dex_cash.toFixed(2)} USDT`;
                document.getElementById('dex-asset-val').innerText = `${state.dex_asset.toFixed(4)} SOL`;

                const totalPnl = state.total_pnl;
                const dailyPnl = state.daily_pnl;
                
                const pnlValEl = document.getElementById('pnl-val');
                pnlValEl.innerText = `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} USDT`;
                pnlValEl.className = `metric-value ${totalPnl >= 0 ? 'text-green' : 'text-red'}`;
                
                const dailyPnlEl = document.getElementById('daily-pnl-val');
                dailyPnlEl.innerText = `Daily P&L: ${dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(4)} USDT`;
                dailyPnlEl.className = `metric-sub ${dailyPnl >= 0 ? 'text-green' : 'text-red'}`;

                // 2. Realism Upgrades Cards
                // Slippage Card
                document.getElementById('slip-val').innerText = `${cumSlipPct >= 0 ? '+' : ''}${cumSlipPct.toFixed(3)}%`;
                document.getElementById('slip-val').className = `metric-value ${totalSlipUsd <= 0.05 ? 'text-green' : 'text-red'}`;
                document.getElementById('slip-sub').innerText = `Total Slippage: $${totalSlipUsd.toFixed(3)} USDT`;

                // Route A Stats
                const routeA = state.route_stats["DEX-BUY_CEX-SELL"] || {win_count: 0, loss_count: 0, total_pnl: 0.0};
                const rA_total = routeA.win_count + routeA.loss_count;
                const rA_wr = rA_total > 0 ? (routeA.win_count / rA_total * 100.0) : 0.0;
                document.getElementById('route-a-pnl').innerText = `${routeA.total_pnl >= 0 ? '+' : ''}${routeA.total_pnl.toFixed(2)} USDT`;
                document.getElementById('route-a-pnl').className = `metric-value ${routeA.total_pnl >= 0 ? 'text-green' : 'text-red'}`;
                document.getElementById('route-a-winrate').innerText = `Win Rate: ${rA_wr.toFixed(1)}% (${routeA.win_count}/${rA_total})`;

                // Route B Stats
                const routeB = state.route_stats["CEX-BUY_DEX-SELL"] || {win_count: 0, loss_count: 0, total_pnl: 0.0};
                const rB_total = routeB.win_count + routeB.loss_count;
                const rB_wr = rB_total > 0 ? (routeB.win_count / rB_total * 100.0) : 0.0;
                document.getElementById('route-b-pnl').innerText = `${routeB.total_pnl >= 0 ? '+' : ''}${routeB.total_pnl.toFixed(2)} USDT`;
                document.getElementById('route-b-pnl').className = `metric-value ${routeB.total_pnl >= 0 ? 'text-green' : 'text-red'}`;
                document.getElementById('route-b-winrate').innerText = `Win Rate: ${rB_wr.toFixed(1)}% (${routeB.win_count}/${rB_total})`;

                // Drawdown & Priority Fees Card
                const peak = state.peak_account_size || state.account_size || 500.0;
                const current = state.account_size || 500.0;
                const dd = peak > 0 ? ((peak - current) / peak * 100.0) : 0.0;
                const totalFees = state.total_priority_fees_usd || 0.0;
                document.getElementById('drawdown-val').innerText = `${dd.toFixed(2)}% DD`;
                document.getElementById('drawdown-val').className = `metric-value ${state.max_drawdown_paused ? 'text-red' : 'text-green'}`;
                document.getElementById('fees-sub').innerText = `Priority Fees: $${totalFees.toFixed(4)} USDT`;

                // 3. Spatial Spreads
                if (dexBuy > 0 && bestBid > 0) {
                    const spreadA = ((bestBid / dexBuy) - 1.0) * 100.0;
                    document.getElementById('spread-a').innerText = `${spreadA >= 0 ? '+' : ''}${spreadA.toFixed(3)}%`;
                    document.getElementById('spread-a').className = `spread-pct ${spreadA >= 0.5 ? 'text-green' : 'text-red'}`;
                    document.getElementById('price-dex-buy').innerText = `$${dexBuy.toFixed(4)}`;
                    document.getElementById('price-cex-sell').innerText = `$${bestBid.toFixed(4)}`;
                }
                
                if (bestAsk > 0 && dexSell > 0) {
                    const spreadB = ((dexSell / bestAsk) - 1.0) * 100.0;
                    document.getElementById('spread-b').innerText = `${spreadB >= 0 ? '+' : ''}${spreadB.toFixed(3)}%`;
                    document.getElementById('spread-b').className = `spread-pct ${spreadB >= 0.5 ? 'text-green' : 'text-red'}`;
                    document.getElementById('price-cex-buy').innerText = `$${bestAsk.toFixed(4)}`;
                    document.getElementById('price-dex-sell').innerText = `$${dexSell.toFixed(4)}`;
                }

                // 4. Fills / Cycles
                document.getElementById('fills-count').innerText = `${state.cycle_count} cycles`;
                const tbody = document.getElementById('trade-history');
                if (state.closed_trades && state.closed_trades.length > 0) {
                    tbody.innerHTML = state.closed_trades.slice().reverse().slice(0, 15).map(trade => {
                        const side = trade.direction;
                        const isRouteA = side === "DEX-BUY_CEX-SELL";
                        const expP = trade.expected_pnl !== undefined ? trade.expected_pnl : trade.pnl_usdt;
                        const actP = trade.actual_pnl !== undefined ? trade.actual_pnl : trade.pnl_usdt;
                        const slipP = trade.slippage_pct !== undefined ? (trade.slippage_pct * 100).toFixed(3) + "%" : "0.0%";
                        return `
                            <tr>
                                <td><code>${trade.cycle_id}</code></td>
                                <td><span class="badge ${isRouteA ? 'route-a' : 'route-b'}">${isRouteA ? 'DEX → CEX' : 'CEX → DEX'}</span></td>
                                <td>${trade.size_usdt.toFixed(1)}</td>
                                <td style="font-family: 'JetBrains Mono', monospace; color: var(--text-muted);">${expP >= 0 ? '+' : ''}${expP.toFixed(3)}</td>
                                <td class="${actP >= 0 ? 'text-green' : 'text-red'} font-mono" style="font-family: 'JetBrains Mono', monospace; font-weight: 600;">
                                    ${actP >= 0 ? '+' : ''}${actP.toFixed(3)}
                                </td>
                                <td class="text-red font-mono" style="font-family: 'JetBrains Mono', monospace;">${slipP}</td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2rem 0;">No arbitrage events logged yet</td></tr>';
                }

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
                    if (line.includes('Completed') || line.includes('Arb Trigger')) levelClass = 'success';
                    return `<div class="console-line ${levelClass}">${line}</div>`;
                }).join('');

                if (isAtBottom) {
                    consoleEl.scrollTop = consoleEl.scrollHeight;
                }
            } catch (err) {
                console.error("Log poll failed", err);
            }
        }

        setInterval(updateDashboard, 1000);
        setInterval(updateLogs, 1000);
        
        updateDashboard();
        updateLogs();

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
        scanner = request.app["scanner"]
        state = load_state()

        # Fetch SOL bids and asks
        bid, ask, _, _ = price_feed.get_best_bid_ask("SOLUSDT")

        # Calculate advanced slippage metrics
        vol = state.total_volume_usdt
        slip_pct = (state.total_slippage_usd / vol * 100.0) if vol > 0 else 0.0

        return web.json_response({
            "state": state.to_dict(),
            "bybit_bid": bid or 0.0,
            "bybit_ask": ask or 0.0,
            "dex_buy": scanner.last_dex_buy,
            "dex_sell": scanner.last_dex_sell,
            "cumulative_slippage_pct": round(slip_pct, 4),
        })
    except Exception as e:
        logger.warning(f"Error compiling api state: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_api_logs(request):
    return web.json_response({"logs": list(recent_logs)})

async def handle_api_rebalance(request):
    try:
        data = await request.json()
        asset = data.get("asset")  # "USDT" or "SOL"
        direction = data.get("direction")  # "CEX_TO_DEX" or "DEX_TO_CEX"
        amount = float(data.get("amount", 0))

        from polymarket_bot.state import rebalance_portfolio
        state = load_state()
        success, message = rebalance_portfolio(state, asset, direction, amount)
        if success:
            logger.info(f"🔄 [Manual Rebalance] {message}")
            return web.json_response({"success": True, "message": message})
        else:
            logger.warning(f"❌ [Rebalance Failed] {message}")
            return web.json_response({"success": False, "error": message}, status=400)
    except Exception as e:
        logger.error(f"Error executing rebalance: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def start_dashboard(price_feed, scanner) -> web.AppRunner:
    """Initialize and start the dashboard web server on port 8080."""
    app = web.Application()
    app["price_feed"] = price_feed
    app["scanner"] = scanner
    
    app.router.add_get("/", handle_dashboard_index)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/api/logs", handle_api_logs)
    app.router.add_post("/api/rebalance", handle_api_rebalance)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    logger.info(f"🟢 Dashboard UI running on http://0.0.0.0:{port}")
    return runner
