import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()

class ClosedTradeLog(Base):
    __tablename__ = "closed_trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    direction = Column(String)
    entry_price = Column(Float)
    exit_price = Column(Float)
    pnl_usd = Column(Float)
    fee_usd = Column(Float)
    opened_at = Column(DateTime)
    closed_at = Column(DateTime)

def main():
    db_url = "postgresql://postgres.vnwlgfbgsqpfpdstheen:AiosTradingEngine2026!@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
    print("Connecting to Supabase on port 5432...")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    trades = session.query(ClosedTradeLog).all()
    print(f"Total trades in Supabase closed_trades table: {len(trades)}")
    
    if len(trades) > 0:
        # Print summary stats
        wins = sum(1 for t in trades if t.pnl_usd > 0)
        losses = sum(1 for t in trades if t.pnl_usd <= 0)
        total_pnl = sum(t.pnl_usd for t in trades)
        total_fees = sum(t.fee_usd for t in trades)
        print(f"Wins: {wins}")
        print(f"Losses: {losses}")
        print(f"Total P&L: ${total_pnl:.2f}")
        print(f"Total Fees: ${total_fees:.2f}")
        
        print("\nFirst 5 trades:")
        for t in trades[:5]:
            print(f"{t.symbol} {t.direction} entry={t.entry_price} exit={t.exit_price} pnl={t.pnl_usd} fee={t.fee_usd}")
            
        print("\nLast 5 trades:")
        for t in trades[-5:]:
            print(f"{t.symbol} {t.direction} entry={t.entry_price} exit={t.exit_price} pnl={t.pnl_usd} fee={t.fee_usd}")

if __name__ == "__main__":
    main()
