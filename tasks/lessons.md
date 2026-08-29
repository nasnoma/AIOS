# Lessons Learned & Operational Rules

## Order Execution Rules
1. **Never Sell at Raw Market/Best Bid**:
   - When closing or rebalancing positions, always place limit sell orders at or above the desired price target (e.g. `cost_basis * 1.015` or higher).
   - Never cross the spread by selling directly into the bid unless explicitly required.
2. **Reconciliation Cost Basis Accuracy**:
   - Always query and bind the exact FIFO purchase price directly from Bybit exchange trade history (`fetch_my_trades`) rather than relying on initial static estimates.
   - Do not display rough profit approximations on the dashboard before Bybit execution receipts are finalized to prevent visual number adjustments.
3. **Hard Profit Floor**:
   - Every completed sell order must yield $\ge \$0.50$ net profit after all maker/taker fees.
