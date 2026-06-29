"""
polymarket_bot/dashboard.py

Embedded aiohttp web server running a dashboard for real-time monitoring.
Features deep navy dark mode, glassmorphic cards, live state updates, and logs.
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

# Setup loguru sink at import or startup
logger.add(log_sink, level="INFO", format="{time:HH:mm:ss} | {level:7} | {message}")

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Bot Dashboard</title>
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

        .status-badge.cooldown {
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
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
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
            font-size: 2rem;
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
            grid-template-columns: 1.8fr 1.2fr;
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
        .badge.sell { background: var(--color-red-glow); color: var(--color-red); }

        .console-card {
            display: flex;
            flex-direction: column;
            height: clamp(200px, 40vh, 450px);
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
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
        }

        .spot-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .spot-info h4 {
            font-size: 0.875rem;
            color: var(--text-muted);
        }

        .spot-val {
            font-size: 1.25rem;
            font-weight: 600;
            margin-top: 0.25rem;
        }

        .spot-mom {
            font-size: 0.75rem;
            font-weight: 600;
        }

        .status-badge.disconnected {
            background-color: rgba(245, 158, 11, 0.15);
            color: var(--color-orange);
            border-color: rgba(245, 158, 11, 0.3);
            box-shadow: 0 0 10px rgba(245, 158, 11, 0.1);
        }

        .last-updated {
            font-size: 0.75rem;
            color: var(--text-muted);
            opacity: 0.6;
        }

        @media (max-width: 640px) {
            header {
                padding: 1rem;
                flex-direction: column;
                gap: 1rem;
                align-items: flex-start;
            }
            main {
                padding: 1rem;
                gap: 1.25rem;
            }
            .spot-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-container">
            <div class="logo-icon">P</div>
            <div class="logo-text">
                <h1>POLYMARKET BOT</h1>
                <span>Intra-Window Momentum Sniping</span>
            </div>
        </div>
        <div style="display:flex; align-items:center; gap:0.75rem;">
            <span id="last-updated" class="last-updated"></span>
            <div id="cooldown-badge" class="status-badge active">
                <div class="pulse-dot"></div>
                <span id="cooldown-text">Active Monitoring</span>
            </div>
        </div>
    </header>

    <main>
        <div class="metrics-grid">
            <div class="card">
                <div class="metric-title">Portfolio Balance</div>
                <div id="cash-value" class="metric-value">$0.00</div>
                <div class="metric-sub">Capital Pool (Paper)</div>
            </div>
            <div class="card">
                <div class="metric-title">Cumulative PnL</div>
                <div id="pnl-value" class="metric-value">$0.00</div>
                <div id="pnl-sub" class="metric-sub">---</div>
            </div>
            <div class="card">
                <div class="metric-title">Win Rate</div>
                <div id="winrate-value" class="metric-value">0.0%</div>
                <div id="winrate-sub" class="metric-sub">0W / 0L</div>
            </div>
            <div class="card">
                <div class="metric-title">Strategy Config &amp; EV</div>
                <div style="font-size: 0.85rem; line-height: 1.8; color: var(--text-muted);">
                    Momentum Floor: <span id="cfg-mom" class="text-green" style="font-weight:600;">$15</span><br>
                    Min Probability: <span id="cfg-conf" class="text-green" style="font-weight:600;">56%</span><br>
                    <span style="border-top: 1px solid var(--border-color); display:block; margin: 0.4rem 0;"></span>
                    Avg Win: <span id="cfg-avg-win" class="text-green" style="font-weight:600;">---</span><br>
                    Avg Loss: <span id="cfg-avg-loss" class="text-red" style="font-weight:600;">---</span><br>
                    <span id="cfg-ev" style="font-size:0.8rem;">EV: <span style="font-weight:600;">---</span></span>
                </div>
            </div>
        </div>

        <div class="dashboard-body">
            <div class="card" style="display: flex; flex-direction: column; gap: 2rem;">
                <div>
                    <div class="section-title">Open Positions</div>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>
                                    <th>Asset</th>
                                    <th>Signal Type</th>
                                    <th>Side</th>
                                    <th>Entry Price</th>
                                    <th>Unrealized PnL</th>
                                </tr>
                            </thead>
                            <tbody id="positions-body">
                                <tr>
                                    <td colspan="5" style="text-align: center; color: var(--text-muted);">No open positions</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <div>
                    <div class="section-title">Recent Trade History</div>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>
                                    <th>Asset</th>
                                    <th>Side</th>
                                    <th>Position Size</th>
                                    <th>Closed At</th>
                                    <th>Realized PnL</th>
                                </tr>
                            </thead>
                            <tbody id="history-body">
                                <tr>
                                    <td colspan="5" style="text-align: center; color: var(--text-muted);">No trade history</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <div style="display: flex; flex-direction: column; gap: 1.5rem;">
                <div class="card">
                    <div class="section-title">Asset Spot Prices</div>
                    <div class="spot-grid">
                        <div class="spot-card">
                            <div class="spot-info">
                                <h4>BTC Price</h4>
                                <div id="btc-price" class="spot-val">---</div>
                            </div>
                            <div id="btc-mom" class="spot-mom">---</div>
                        </div>
                        <div class="spot-card">
                            <div class="spot-info">
                                <h4>ETH Price</h4>
                                <div id="eth-price" class="spot-val">---</div>
                            </div>
                            <div id="eth-mom" class="spot-mom">---</div>
                        </div>
                    </div>
                </div>

                <div class="card console-card">
                    <div class="section-title">Live Bot Console</div>
                    <div id="console-output" class="console-body"></div>
                </div>
            </div>
        </div>
    </main>

    <script>
        let failureCount = 0;
        let lastUpdatedAt = null;

        function tickLastUpdated() {
            const el = document.getElementById('last-updated');
            if (!lastUpdatedAt || failureCount >= 2) return;
            const secs = Math.round((Date.now() - lastUpdatedAt) / 1000);
            el.innerText = secs < 5 ? 'Updated just now' : `Updated ${secs}s ago`;
        }

        async function fetchState() {
            try {
                const res = await fetch('/api/state');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                failureCount = 0;
                lastUpdatedAt = Date.now();
                document.getElementById('last-updated').innerText = 'Updated just now';
                updateDashboard(data);
            } catch (err) {
                failureCount++;
                console.error("Failed to fetch state:", err);
                if (failureCount >= 2) {
                    const badge = document.getElementById('cooldown-badge');
                    const text = document.getElementById('cooldown-text');
                    badge.className = 'status-badge disconnected';
                    text.innerText = '\u26a0 Disconnected';
                }
            }
        }

        async function fetchLogs() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                const container = document.getElementById('console-output');
                const isScrolledToBottom = container.scrollHeight - container.clientHeight <= container.scrollTop + 30;

                container.innerHTML = data.logs.map(log => {
                    let logClass = 'info';
                    if (log.includes('WARNING')) logClass = 'warning';
                    if (log.includes('SUCCESS') || log.includes('Resolved') || log.includes('✅')) logClass = 'success';
                    if (log.includes('ERROR') || log.includes('failed') || log.includes('❌')) logClass = 'error';
                    return `<div class="console-line ${logClass}">${log}</div>`;
                }).join('');

                if (isScrolledToBottom) container.scrollTop = container.scrollHeight;
            } catch (err) {
                console.error("Failed to fetch logs:", err);
            }
        }

        function updateDashboard(data) {
            document.getElementById('cash-value').innerText = `$${data.state.cash.toFixed(2)}`;
            const totalPnL = data.state.total_pnl;
            const pnlValEl = document.getElementById('pnl-value');
            pnlValEl.innerText = `${totalPnL >= 0 ? '+' : ''}$${totalPnL.toFixed(2)}`;
            pnlValEl.className = `metric-value ${totalPnL >= 0 ? 'text-green' : 'text-red'}`;
            
            const dailyPnL = data.state.daily_pnl;
            document.getElementById('pnl-sub').innerText = `Daily PnL: ${dailyPnL >= 0 ? '+' : ''}$${dailyPnL.toFixed(2)}`;
            document.getElementById('pnl-sub').className = `metric-sub ${dailyPnL >= 0 ? 'text-green' : 'text-red'}`;

            const wins = data.state.win_count;
            const losses = data.state.loss_count;
            const winRate = (wins + losses) > 0 ? (wins / (wins + losses) * 100) : 0;
            document.getElementById('winrate-value').innerText = `${winRate.toFixed(1)}%`;
            document.getElementById('winrate-sub').innerText = `${wins}W / ${losses}L`;

            // Update config card from live API values
            if (data.config) {
                document.getElementById('cfg-mom').innerText = `$${data.config.momentum_floor}`;
                document.getElementById('cfg-conf').innerText = `${(data.config.min_prob * 100).toFixed(0)}%`;
            }

            // Update avg win / loss / EV
            const avgWin = data.state.avg_win_usd || 0;
            const avgLoss = data.state.avg_loss_usd || 0;
            const wr = data.state.win_count / Math.max(1, data.state.win_count + data.state.loss_count);
            document.getElementById('cfg-avg-win').innerText = avgWin !== 0 ? `+$${avgWin.toFixed(2)}` : '---';
            document.getElementById('cfg-avg-loss').innerText = avgLoss !== 0 ? `$${avgLoss.toFixed(2)}` : '---';
            if (avgWin !== 0 && avgLoss !== 0) {
                const ev = (wr * avgWin) + ((1 - wr) * avgLoss);
                const evEl = document.getElementById('cfg-ev');
                evEl.innerHTML = `EV/trade: <span style="font-weight:600;" class="${ev >= 0 ? 'text-green' : 'text-red'}">${ev >= 0 ? '+' : ''}$${ev.toFixed(3)}</span>`;
            } 
            
            const badge = document.getElementById('cooldown-badge');
            const text = document.getElementById('cooldown-text');
            if (data.cooldown_remaining > 0) {
                badge.className = 'status-badge cooldown';
                text.innerText = `Cooldown: ${Math.round(data.cooldown_remaining)}s`;
            } else {
                badge.className = 'status-badge active';
                text.innerText = 'Active Monitoring';
            }

            const positionsBody = document.getElementById('positions-body');
            if (data.state.positions && data.state.positions.length > 0) {
                positionsBody.innerHTML = data.state.positions.map(p => {
                    const price = p.side === 'YES' ? p.entry_price_yes : (p.side === 'NO' ? p.entry_price_no : 0.0);
                    return `<tr><td style="font-weight: 600;">${p.asset}</td><td>${p.signal_type}</td><td><span class="badge ${p.side === 'YES' ? 'buy' : 'sell'}">${p.side}</span></td><td>$${price.toFixed(3)}</td><td class="${p.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}" style="font-weight: 600;">${p.unrealized_pnl >= 0 ? '+' : ''}$${p.unrealized_pnl.toFixed(2)}</td></tr>`;
                }).join('');
            } else {
                positionsBody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No open positions</td></tr>`;
            }

            const historyBody = document.getElementById('history-body');
            const lastTrades = data.state.closed_trades ? data.state.closed_trades.slice(-5).reverse() : [];
            if (lastTrades.length > 0) {
                historyBody.innerHTML = lastTrades.map(t => `<tr><td style="font-weight: 600;">${t.asset}</td><td><span class="badge ${t.side === 'YES' ? 'buy' : 'sell'}">${t.side}</span></td><td>$${t.size_usd.toFixed(2)}</td><td>${new Date(t.closed_at).toLocaleTimeString()}</td><td class="${t.pnl_usd >= 0 ? 'text-green' : 'text-red'}" style="font-weight: 600;">${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(2)}</td></tr>`).join('');
            } else {
                historyBody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No trade history</td></tr>`;
            }

            if (data.market_status) {
                ['BTC', 'ETH'].forEach(asset => {
                    const m = data.market_status[asset];
                    if (m) {
                        document.getElementById(`${asset.toLowerCase()}-price`).innerText = m.price ? `$${m.price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : '---';
                        const momEl = document.getElementById(`${asset.toLowerCase()}-mom`);
                        if (m.momentum !== null && m.momentum !== undefined) {
                            momEl.innerText = `${m.momentum >= 0 ? '+' : ''}$${m.momentum.toFixed(2)}`;
                            momEl.className = `spot-mom ${m.momentum >= 0 ? 'text-green' : 'text-red'}`;
                        }
                    }
                });
            }
        }

        setInterval(fetchState, 3000);
        setInterval(fetchLogs, 3000);
        fetchState();
        fetchLogs();
    </script>
</body>
</html>
"""

async def handle_dashboard_index(request):
    return web.Response(text=_INDEX_HTML, content_type="text/html")

async def handle_api_state(request):
    try:
        # Load local paper/live state
        state = load_state()
        
        # Access active pricing and cooldown from current app instance
        price_feed = request.app["price_feed"]
        
        # Calculate expected cooldown
        consecutive_losses = 0
        for t in reversed(state.closed_trades):
            pnl = t.get("pnl_usd")
            if pnl is not None:
                if pnl < 0:
                    consecutive_losses += 1
                else:
                    break

        cooldown_remaining = 0.0
        if consecutive_losses >= 2 and state.closed_trades:
            last_closed_str = state.closed_trades[-1].get("closed_at")
            if last_closed_str:
                try:
                    last_closed_dt = datetime.fromisoformat(last_closed_str.replace("Z", "+00:00"))
                    now_dt = datetime.now(timezone.utc)
                    elapsed = (now_dt - last_closed_dt).total_seconds()
                    if elapsed < 600.0:
                        cooldown_remaining = 600.0 - elapsed
                except Exception:
                    pass

        # Calculate unrealized P&Ls for dashboard UI
        from polymarket_bot.execution import compute_unrealized_pnl
        positions_with_pnl = []
        for pos in state.open_positions:
            unrealized = await compute_unrealized_pnl(pos, price_feed)
            pos_dict = dict(pos)
            pos_dict["unrealized_pnl"] = unrealized
            positions_with_pnl.append(pos_dict)

        # Assemble market prices + strike based momentum
        from polymarket_bot.market import get_current_window
        window = get_current_window()
        
        market_status = {}
        for asset in ["BTC", "ETH"]:
            price = price_feed.get_latest(asset)
            strike = price_feed.get_strike(asset, window.window_start)
            momentum = (price - strike) if (price and strike) else None
            market_status[asset] = {
                "price": price,
                "momentum": momentum
            }

        state_dict = state.to_dict()
        state_dict["positions"] = positions_with_pnl

        return web.json_response({
            "state": state_dict,
            "cooldown_remaining": cooldown_remaining,
            "market_status": market_status,
            "config": {
                "momentum_floor": getattr(settings, "min_momentum_threshold", 15),
                "min_prob": getattr(settings, "min_confidence", 0.56),
            }
        })
    except Exception as e:
        logger.warning(f"Error compiling api state: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_api_logs(request):
    return web.json_response({"logs": list(recent_logs)})

async def start_dashboard(price_feed) -> web.AppRunner:
    """Initialize and start the dashboard web server on port 8080 or env PORT."""
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
