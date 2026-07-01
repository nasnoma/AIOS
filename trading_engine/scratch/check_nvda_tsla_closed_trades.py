import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from trading_engine.storage import db

def main():
    with db.get_session() as session:
        print("\n--- QUERYING CLOSED TRADES FOR NVDA AND TSLA ON JUNE 23 ---")
        closed_trades = session.query(db.ClosedTradeLog).filter(
            (db.ClosedTradeLog.symbol.like("%NVDA%")) | 
            (db.ClosedTradeLog.symbol.like("%TSLA%")) | 
            (db.ClosedTradeLog.symbol.like("%AAPL%"))
        ).filter(db.ClosedTradeLog.opened_at >= '2026-06-22').order_by(db.ClosedTradeLog.opened_at.desc()).all()
        
        for t in closed_trades:
            print("="*80)
            print(f"ID={t.id} | Symbol={t.symbol} | Dir={t.direction} | Entry={t.entry_price} | Exit={t.exit_price} | Size={t.size_usd} | PnL={t.pnl_usd} | Fee={t.fee_usd} | Opened={t.opened_at} | Closed={t.closed_at} | Reason={t.exit_reason}")

        print("\n--- QUERYING ALL ORDER AUDIT LOGS FOR JUNE 23 ---")
        orders = session.query(db.OrderAuditLog).filter(
            (db.OrderAuditLog.symbol.like("%NVDA%")) | 
            (db.OrderAuditLog.symbol.like("%TSLA%")) | 
            (db.OrderAuditLog.symbol.like("%AAPL%"))
        ).filter(db.OrderAuditLog.timestamp >= '2026-06-23').order_by(db.OrderAuditLog.timestamp.desc()).all()
        print(f"Found {len(orders)} order logs:")
        for o in orders[:20]:
            print(f"ID={o.id} | Time={o.timestamp} | Symbol={o.symbol} | Side={o.side} | Qty={o.qty} | Price={o.price} | Status={o.status} | Err={o.error_message}")
            print(f"  Payload: {o.payload}")

if __name__ == "__main__":
    main()
