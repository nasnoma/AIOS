import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()

class ClosedTradeLog(Base):
    __tablename__ = "closed_trades"
    id = Column(Integer, primary_key=True)
    entry_price = Column(Float)

def main():
    db_url = "postgresql://postgres.vnwlgfbgsqpfpdstheen:AiosTradingEngine2026!@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
    print("Connecting to Supabase on port 5432...")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    print("Deleting matching mock trades...")
    deleted = session.query(ClosedTradeLog).filter(ClosedTradeLog.entry_price == 65000.0).delete()
    session.commit()
    print(f"Successfully deleted {deleted} mock trades from Supabase!")

if __name__ == "__main__":
    main()
