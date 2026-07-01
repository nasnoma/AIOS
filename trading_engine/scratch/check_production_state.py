import sqlalchemy
from sqlalchemy import create_engine, Column, String, Text, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
import json

Base = declarative_base()

class PortfolioState(Base):
    __tablename__ = "portfolio_states"
    key = Column(String(50), primary_key=True)
    state_json = Column(Text)
    updated_at = Column(DateTime)

def main():
    db_url = "postgresql://postgres.vnwlgfbgsqpfpdstheen:AiosTradingEngine2026!@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
    print("Connecting to Supabase...")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    state = session.query(PortfolioState).filter_by(key="live").first()
    if state and state.state_json:
        parsed = json.loads(state.state_json)
        print("\n--- Live State from DB ---")
        print(f"Account Size: {parsed.get('account_size')}")
        print(f"Cash: {parsed.get('cash')}")
        print(f"Total P&L: {parsed.get('total_pnl')}")
        print(f"Total Fees: {parsed.get('total_fees')}")
        print(f"Win Count: {parsed.get('win_count')}")
        print(f"Loss Count: {parsed.get('loss_count')}")
        print("\nOpen Positions:")
        for p in parsed.get("positions", []):
            print(f"  {p.get('symbol')} {p.get('direction')} size=${p.get('size_usd')} entry={p.get('entry_price')}")
        print("\nLast 5 Closed Trades:")
        for t in parsed.get("closed_trades", [])[-5:]:
            print(f"  {t.get('symbol')} pnl=${t.get('pnl_usd')} entry={t.get('entry_price')} exit={t.get('exit_price')}")
    else:
        print("No live state found.")

if __name__ == "__main__":
    main()
