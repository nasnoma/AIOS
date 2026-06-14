import sqlite3
import json

def check_db():
    conn = sqlite3.connect("trading_engine.db")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print(f"Tables in DB: {[t[0] for t in tables]}")
        
        # Check orders table
        if ("order_audit_logs",) in tables:
            cursor.execute("SELECT * FROM order_audit_logs ORDER BY id DESC LIMIT 5")
            rows = cursor.fetchall()
            print("\nLast 5 orders in order_audit_logs:")
            for r in rows:
                print(r)
            
        # Check API calls
        if ("api_audit_logs",) in tables:
            cursor.execute("SELECT * FROM api_audit_logs ORDER BY id DESC LIMIT 5")
            rows = cursor.fetchall()
            print("\nLast 5 API calls in api_audit_logs:")
            for r in rows:
                print(r)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    check_db()
