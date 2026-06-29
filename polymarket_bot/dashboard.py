"""
polymarket_bot/dashboard.py

Embedded aiohttp web server running a premium quantitative dashboard for Bybit SOL/USDT Grid Market Maker.
Features deep navy dark mode, glassmorphic cards, live order ladder, inventory ratio, and console logs.
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
    <title>Bybit SOL/USDT Grid Market Maker</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-main: #04060d;
            --bg-card: rgba(10, 15, 30, 0.45);
            --bg-card-hover: rgba(16, 25, 48, 0.6);
            --border-color: rgba(255, 255, 255, 0.06);
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
                radial-gradient(circle at 10% 20%, rgba(6, 182, 212, 0.04) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(99, 102, 241, 0.04) 0%, transparent 40%);
            background-attachment: fixed;
        }

        header {
            padding: 1.5rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            background: rgba(4, 6, 13, 0.85);
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
            box-shadow: 0 0 15px rgba(6, 182, 212, 0.3);
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
        }

        .status-badge.active {
            background-color: var(--color-green-glow);
            color: var(--color-green);
            border-color: rgba(16, 185, 129, 0.2);
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
        }

        .card:hover {
            transform: translateY(-2px);
            border-color: rgba(255, 255, 255, 0.1);
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
        }

        .text-green { color: var(--color-green); }
        .text-red { color: var(--color-red); }

        /* Inventory Allocation Progress Bar */
        .allocation-container {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            margin-top: 0.5rem;
        }

        .allocation-bar-bg {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 0.5rem;
            height: 0.75rem;
            width: 100%;
            overflow: hidden;
            position: relative;
        }

        .allocation-bar-fill {
            background: linear-gradient(90deg, var(--color-primary), #6366f1);
            height: 100%;
            width: 50%;
            transition: width 0.5s ease-in-out;
        }

        .allocation-labels {
            display: flex;
            justify-content: space-between;
            font-size: 0.75rem;
            color: var(--text-muted);
        }

        .dashboard-body {
            display: grid;
            grid-template-columns: 1fr 1.2fr 1fr;
            gap: 1.5rem;
        }

        @media (max-width: 1200px) {
            .dashboard-body {
                grid-template-columns: 1fr;
            }
        }

        .section-title {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        /* Grid Ladder Board */
        .ladder-list {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            font-family: 'JetBrains Mono', monospace;
        }

        .ladder-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.65rem 1rem;
            border-radius: 0.5rem;
            border: 1px solid transparent;
            font-size: 0.85rem;
        }

        .ladder-row.sell {
            background: rgba(244, 63, 94, 0.05);
            border-color: rgba(244, 63, 94, 0.12);
            color: #fca5a5;
        }

        .ladder-row.buy {
            background: rgba(16, 185, 129, 0.05);
            border-color: rgba(16, 185, 129, 0.12);
            color: #a7f3d0;
        }

        .ladder-row.center {
            background: rgba(255, 255, 255, 0.02);
            border-color: rgba(255, 255, 255, 0.08);
            color: var(--text-main);
            font-weight: 600;
            text-align: center;
            justify-content: center;
            gap: 0.5rem;
        }

        /* Trade History & Logs */
        .table-container {
            width: 100%;
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.85rem;
        }

        th {
            padding: 0.75rem 1rem;
            color: var(--text-muted);
            font-weight: 600;
            font-size: 0.75rem;
            text-transform: uppercase;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 0.85rem 1rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.02);
        }

        .badge {
            display: inline-block;
            padding: 0.2rem 0.4rem;
            border-radius: 0.25rem;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
        }

        .badge.buy { background: var(--color-green-glow); color: var(--color-green); }
        .badge.sell { background: var(--color-red-glow); color: var(--color-red); }

        .console-card {
            display: flex;
            flex-direction: column;
            height: clamp(300px, 60vh, 600px);
        }

        .console-body {
            flex: 1;
            background: #020306;
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 0.5rem;
            padding: 1rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            line-height: 1.5;
            overflow-y: auto;
            white-space: pre-wrap;
            color: #cbd5e1;
        }

        .console-line {
            margin-bottom: 0.35rem;
            border-left: 2px solid transparent;
            padding-left: 0.5rem;
        }

        .console-line.info { border-left-color: var(--color-primary); }
        .console-line.warning { border-left-color: var(--color-orange); color: var(--color-orange); }
        .console-line.success { border-left-color: var(--color-green); color: var(--color-green); }
        .console-line.error { border-left-color: var(--color-red); color: var(--color-red); }

        @media (max-width: 768px) {
            main {
                padding: 1rem;
            }
            .metrics-grid {
                grid-template-columns: 1fr 1fr;
            }
        }
        @media (max-width: 480px) {
            .metrics-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-container">
            <div class="logo-icon">▲</div>
            <div class="logo-text">
                <h1>Bybit SOL Grid MM</h1>
                <span>Halal Spot Maker Engine</span>
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
                <div class="metric-title">Equity (USDT)</div>
                <div class="metric-value" id="equity-val">---</div>
                <div class="metric-sub" id="equity-sub">Cash + Asset Valuation</div>
            </div>
            <div class="card">
                <div class="metric-title">USDT Balance</div>
                <div class="metric-value" id="cash-val">---</div>
                <div class="metric-sub">Free Spot Wallet cash</div>
            </div>
            <div class="card">
                <div class="metric-title">SOL Inventory</div>
                <div class="metric-value" id="asset-val">---</div>
                <div class="metric-sub" id="avg-entry-val">Avg entry: ---</div>
            </div>
            <div class="card">
                <div class="metric-title">Inventory Allocation</div>
                <div class="allocation-container">
                    <div class="allocation-bar-bg">
                        <div class="allocation-bar-fill" id="alloc-bar"></div>
                    </div>
                    <div class="allocation-labels">
                        <span id="label-usdt">50% USDT</span>
                        <span id="label-sol">50% SOL</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- 3-Column Layout -->
        <div class="dashboard-body">
            <!-- 1. Grid Order Book Ladder -->
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="section-title">
                    <span>Order Ladder</span>
                    <span style="font-size: 0.8rem; font-weight: normal; color: var(--text-muted);" id="last-updated">Updated just now</span>
                </div>
                <div class="ladder-list" id="ladder-board">
                    <div style="text-align: center; color: var(--text-muted); font-size: 0.85rem; padding: 2rem 0;">No active resting orders</div>
                </div>
            </div>

            <!-- 2. Recent Fills / Closed Trades -->
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="section-title">
                    <span>Recent Completed Fills</span>
                    <span style="font-size: 0.8rem; color: var(--text-muted);" id="fills-count">0 completed</span>
                </div>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Fill price</th>
                                <th>Side</th>
                                <th>Size</th>
                                <th>PnL (USDT)</th>
                            </tr>
                        </thead>
                        <tbody id="trade-history">
                            <tr>
                                <td colspan="4" style="text-align: center; color: var(--text-muted); padding: 2rem 0;">No fills recorded yet</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 3. Logs Console -->
            <div class="card console-card">
                <div class="section-title">
                    <span>Real-time Grid Logs</span>
                </div>
                <div class="console-body" id="log-console">
                    <div class="console-line info">Waiting for logs...</div>
                </div>
            </div>
        </div>
    </main>

    <script>
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
                const midPrice = data.mid_price;
                const resPrice = data.reservation_price;

                // Basic Stats
                document.getElementById('equity-val').innerText = `${state.account_size.toFixed(2)} USDT`;
                document.getElementById('cash-val').innerText = `${state.cash.toFixed(2)} USDT`;
                document.getElementById('asset-val').innerText = `${state.asset_balance.toFixed(4)} SOL`;
                document.getElementById('avg-entry-val').innerText = `Avg Entry: $${state.avg_buy_price.toFixed(2)}`;

                // Allocation ratio
                const solVal = state.asset_balance * midPrice;
                const totalEquity = state.cash + solVal;
                const solPct = totalEquity > 0 ? (solVal / totalEquity * 100) : 0;
                const usdtPct = 100 - solPct;

                document.getElementById('alloc-bar').style.width = `${solPct}%`;
                document.getElementById('label-usdt').innerText = `${usdtPct.toFixed(0)}% USDT`;
                document.getElementById('label-sol').innerText = `${solPct.toFixed(0)}% SOL`;

                // Order Ladder board
                const ladderBoard = document.getElementById('ladder-board');
                
                if (state.open_grid_orders && state.open_grid_orders.length > 0) {
                    // Sort open grid orders by price descending
                    const sortedOrders = state.open_grid_orders.slice().sort((a, b) => b.price - a.price);
                    
                    const sells = sortedOrders.filter(o => o.side === 'sell');
                    const buys = sortedOrders.filter(o => o.side === 'buy');

                    let html = '';
                    
                    // Render Sell Orders
                    sells.forEach(o => {
                        html += `
                            <div class="ladder-row sell">
                                <span>SELL</span>
                                <span>$${o.price.toFixed(4)}</span>
                                <span>${o.size.toFixed(4)} SOL</span>
                            </div>
                        `;
                    });

                    // Render Price Center
                    html += `
                        <div class="ladder-row center">
                            <div>SOL Price: <span style="color: var(--color-primary); font-size: 1.05rem;">$${midPrice.toFixed(4)}</span></div>
                        </div>
                    `;

                    // Render Buy Orders
                    buys.forEach(o => {
                        html += `
                            <div class="ladder-row buy">
                                <span>BUY</span>
                                <span>$${o.price.toFixed(4)}</span>
                                <span>${o.size.toFixed(4)} SOL</span>
                            </div>
                        `;
                    });

                    ladderBoard.innerHTML = html;
                } else {
                    ladderBoard.innerHTML = `<div style="text-align: center; color: var(--text-muted); font-size: 0.85rem; padding: 2rem 0;">No active resting orders</div>`;
                }

                // Recent Fills
                document.getElementById('fills-count').innerText = `${state.cycle_count} fills`;
                const tbody = document.getElementById('trade-history');
                if (state.closed_trades && state.closed_trades.length > 0) {
                    tbody.innerHTML = state.closed_trades.slice().reverse().slice(0, 15).map(trade => {
                        const side = trade.direction;
                        const isBuy = side === "BUY";
                        return `
                            <tr>
                                <td><code>$${trade.leg1_price.toFixed(4)}</code></td>
                                <td><span class="badge ${isBuy ? 'buy' : 'sell'}">${side}</span></td>
                                <td>${trade.actual_edge_pct.toFixed(4)} SOL</td>
                                <td class="${trade.pnl_usdt >= 0 ? 'text-green' : 'text-red'} font-mono">
                                    ${isBuy ? '---' : `${trade.pnl_usdt >= 0 ? '+' : ''}${trade.pnl_usdt.toFixed(4)}`}
                                </td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 2rem 0;">No fills recorded yet</td></tr>';
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
                    if (line.includes('Fill]') || line.includes('Completed!') || line.includes('Placed') || line.includes('Live')) levelClass = 'success';
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
        state = load_state()

        # Fetch SOL mid price
        bid, ask, _, _ = price_feed.get_best_bid_ask("SOLUSDT")
        mid_price = (bid + ask) / 2.0 if (bid and ask) else 0.0

        # Calculate shaded reservation price
        sol_value = state.asset_balance * mid_price
        total_equity = state.cash + sol_value
        current_inv_ratio = sol_value / total_equity if total_equity > 0 else 0.5
        inv_imbalance = settings.inventory_target_pct - current_inv_ratio
        skew = inv_imbalance * settings.inventory_shading_factor
        reservation_price = mid_price * (1.0 + skew)

        market_status = {}
        for asset in ["SOLUSDT"]:
            b, a, _, _ = price_feed.get_best_bid_ask(asset)
            market_status[asset] = {"bid": b, "ask": a}

        return web.json_response({
            "state": state.to_dict(),
            "mid_price": mid_price,
            "reservation_price": reservation_price,
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
