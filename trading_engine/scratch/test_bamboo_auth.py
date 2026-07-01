import sys
from pathlib import Path

PROJECT_ROOT = Path("/Users/nasir.noma/claude_projects/AIOS")
sys.path.insert(0, str(PROJECT_ROOT))

from trading_engine.utils.bamboo_client import BambooClient
from trading_engine.config import settings

def test():
    print("--- Bamboo Diagnostic Test ---")
    print(f"Base URL: {settings.bamboo_base_url}")
    print(f"Username: {settings.bamboo_username}")
    print(f"User ID (x-user-id): {settings.bamboo_user_id}")
    print(f"Subject Type: {settings.bamboo_subject_type}")
    print(f"API Key: {settings.bamboo_api_key[:5] + '...' if settings.bamboo_api_key else 'Empty'}")
    
    client = BambooClient()
    try:
        token = client.get_client_token()
        print(f"Access Token: {token[:15]}...")
        
        print("\nRequesting NG portfolio breakdown...")
        breakdown = client.get_portfolio_breakdown(asset_class="NGX_STOCK")
        print("Success! Response breakdown:")
        print(breakdown)
        
    except Exception as e:
        print(f"\nAuthentication or Request failed: {e}")

if __name__ == "__main__":
    test()
